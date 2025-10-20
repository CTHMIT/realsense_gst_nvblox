#!/usr/bin/env python3
"""RealSense Virtual Camera Receiver with Visualization.

Receives video streams and IMU data, reconstructs a complete virtual RealSense
camera, and publishes all data to ROS2 topics compatible with isaac_ros_nvblox.

Features:
- Video stream reception (depth, color, IR1, IR2)
- IMU data reception and publishing
- TF tree publishing (camera frames + odom)
- Odometry publishing (for navigation stack)
- Optional live visualization of received streams
- Full compatibility with isaac_ros_nvblox
- Tmux-based stream management (like sender)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None
    print("Warning: OpenCV not available. --show-views will be disabled.")

# Type hints only during type checking
if TYPE_CHECKING:
    import numpy.typing as npt

try:
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import Imu
    from std_msgs.msg import Header
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster
except ImportError:
    print("Error: ROS2 not found. Source your ROS2 installation:")
    print("  source /opt/ros/humble/setup.bash")
    sys.exit(1)

from gst_realsense_launch.rs_common import CameraIntrinsics, ConfigLoader
from gst_realsense_launch.rs_core import StreamStrategyFactory


class TmuxSessionManager:
    """Manages tmux session for multiple GStreamer receiver pipelines."""

    def __init__(self, session_name: str = "realsense_receiver"):
        self.session_name = session_name
        self.window_count = 0
        self._check_tmux()
        self._create_session()

    def _check_tmux(self):
        """Check if tmux is available."""
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("tmux is not installed. Please install tmux: sudo apt install tmux")

    def _create_session(self):
        """Create tmux session if it doesn't exist."""
        # Check if session already exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )

        if result.returncode != 0:
            # Create new detached session
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name, "-n", "control"], check=True
            )
            print(f"✓ Created tmux session '{self.session_name}'")
        else:
            print(f"✓ Using existing tmux session '{self.session_name}'")

    def create_window(self, window_name: str, command: str):
        """Create a new tmux window and run command in it.

        Args:
            window_name: Name for the tmux window
            command: Command to execute in the window
        """
        self.window_count += 1

        # Create new window
        subprocess.run(
            [
                "tmux",
                "new-window",
                "-t",
                f"{self.session_name}:{self.window_count}",
                "-n",
                window_name,
            ],
            check=True,
        )

        # Send command to the window
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{self.session_name}:{window_name}",
                command,
                "C-m",  # Enter key
            ],
            check=True,
        )

        print(f"  ✓ Created window '{window_name}' in tmux")

    def attach_info(self):
        """Display instructions for attaching to the tmux session."""
        print(f"\nTo view the streams, attach to tmux session:")
        print(f"  tmux attach -t {self.session_name}")
        print(f"\nTmux navigation:")
        print(f"  Ctrl+b n : next window")
        print(f"  Ctrl+b p : previous window")
        print(f"  Ctrl+b [0-9] : select window by number")
        print(f"  Ctrl+b d : detach from session")
        print(f"  Ctrl+b & : kill current window")

    def kill_session(self):
        """Kill the entire tmux session."""
        subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
        print(f"✓ Killed tmux session '{self.session_name}'")


class VirtualRealSenseNode(Node):
    """ROS2 Node that creates a virtual RealSense camera.

    This node receives network streams and IMU data, then publishes them
    to standard ROS2 topics that mimic a real RealSense camera. It's fully
    compatible with isaac_ros_nvblox and other ROS2 perception packages.
    """

    def __init__(
        self,
        camera_name: str,
        imu_port: int,
        config_loader: ConfigLoader,
        receiver_config: dict,
    ):
        """Initialize the virtual RealSense camera node.

        Args:
            camera_name: The name of the camera.
            imu_port: The port to listen for IMU data on.
            config_loader: The configuration loader.
            receiver_config: The receiver configuration.
        """
        super().__init__("virtual_realsense_camera")

        self.camera_name = camera_name
        self.imu_port = imu_port
        self.config_loader = config_loader
        self.receiver_config = receiver_config

        # QoS profile matching realsense2_camera driver
        self.qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            durability=DurabilityPolicy.VOLATILE,
        )

        # Create publishers
        self._create_publishers()

        # TF broadcasters
        self.tf_broadcaster = TransformBroadcaster(self)
        self.static_tf_broadcaster = StaticTransformBroadcaster(self)

        # UDP socket for IMU data
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # RealSense network streaming requires accepting remote connections
        # Security note: For production, set local_ip to specific interface IP
        # For local testing only: set local_ip to 127.0.0.1
        bind_address = self.receiver_config.get(
            "local_ip", "0.0.0.0"
        )  # nosec B104 - required for remote streaming
        self.socket.bind((bind_address, imu_port))
        self.socket.settimeout(1.0)

        # Calibration data
        self.intrinsics: dict = {}
        self.extrinsics: dict = {}

        # Odometry state (for isaac_ros_nvblox integration)
        self.odom_x: float = 0.0
        self.odom_y: float = 0.0
        self.odom_theta: float = 0.0
        self.last_odom_time = self.get_clock().now()

        # Start receiver thread
        self.running = True
        self.receiver_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receiver_thread.start()

        # TF and odometry timers
        self.tf_timer = self.create_timer(1.0 / 30.0, self._publish_tf)

        if self.receiver_config.get("publish_odom", True):
            self.odom_timer = self.create_timer(1.0 / 50.0, self._publish_odometry)

        # Publish static transforms
        self._publish_static_transforms()

        self.get_logger().info(f'Virtual RealSense camera "{camera_name}" initialized')
        self.get_logger().info(f"Listening for IMU data on {bind_address}:{imu_port}")
        if bind_address == "0.0.0.0":
            self.get_logger().warn(
                "IMU receiver is listening on all interfaces. "
                "Set local_ip in config.yaml to restrict access."
            )

    def _create_publishers(self):
        """Create all ROS2 publishers for camera streams and IMU."""
        # IMU publisher
        self.imu_pub = self.create_publisher(Imu, f"/{self.camera_name}/imu", self.qos)

        # Odometry publisher (for navigation)
        if self.receiver_config.get("publish_odom", True):
            self.odom_pub = self.create_publisher(Odometry, f"/{self.camera_name}/odom", self.qos)

        self.get_logger().info("Publishers created for camera topics")

    def _receive_loop(self):
        """Receive UDP packets with IMU and calibration data."""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(65536)
                message = json.loads(data.decode("utf-8"))

                msg_type = message.get("type")

                if msg_type == "imu":
                    self._handle_imu_data(message)
                elif msg_type == "calibration":
                    self._handle_calibration_data(message)

            except TimeoutError:
                continue
            except json.JSONDecodeError as e:
                self.get_logger().warn(f"Invalid JSON: {e}")
            except Exception as e:
                self.get_logger().error(f"Error receiving data: {e}")

    def _handle_imu_data(self, data: dict):
        """Process and publish IMU data."""
        try:
            imu_msg = Imu()
            imu_msg.header = Header()
            imu_msg.header.stamp = self.get_clock().now().to_msg()
            imu_msg.header.frame_id = f"{self.camera_name}_imu_optical_frame"

            # Linear acceleration
            accel = data.get("accel", {})
            imu_msg.linear_acceleration.x = accel.get("x", 0.0)
            imu_msg.linear_acceleration.y = accel.get("y", 0.0)
            imu_msg.linear_acceleration.z = accel.get("z", 0.0)

            # Angular velocity
            gyro = data.get("gyro", {})
            imu_msg.angular_velocity.x = gyro.get("x", 0.0)
            imu_msg.angular_velocity.y = gyro.get("y", 0.0)
            imu_msg.angular_velocity.z = gyro.get("z", 0.0)

            # Covariance (uncertainty estimates)
            imu_msg.linear_acceleration_covariance = [
                0.01,
                0.0,
                0.0,
                0.0,
                0.01,
                0.0,
                0.0,
                0.0,
                0.01,
            ]
            imu_msg.angular_velocity_covariance = [0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.01]
            imu_msg.orientation_covariance[0] = -1.0  # No orientation

            self.imu_pub.publish(imu_msg)

        except Exception as e:
            self.get_logger().error(f"Error publishing IMU: {e}")

    def _handle_calibration_data(self, data: dict):
        """Store camera calibration data."""
        self.intrinsics = data.get("intrinsics", {})
        self.extrinsics = data.get("extrinsics", {})
        self.get_logger().info("Received camera calibration data")

    def _publish_static_transforms(self):
        """Publish static transforms for camera structure.

        Creates the standard RealSense frame tree that's expected by
        ROS2 perception algorithms.
        """
        timestamp = self.get_clock().now().to_msg()
        transforms = []

        # Camera base frames (physical sensor positions)
        base_frames = [
            ("depth_frame", [0.0, 0.0, 0.0]),
            ("color_frame", [0.015, 0.0, 0.0]),  # 15mm offset
            ("infra1_frame", [0.0, 0.0, 0.0]),
            ("infra2_frame", [0.050, 0.0, 0.0]),  # 50mm stereo baseline
        ]

        for frame_name, translation in base_frames:
            t = TransformStamped()
            t.header.stamp = timestamp
            t.header.frame_id = f"{self.camera_name}_link"
            t.child_frame_id = f"{self.camera_name}_{frame_name}"
            t.transform.translation.x = translation[0]
            t.transform.translation.y = translation[1]
            t.transform.translation.z = translation[2]
            t.transform.rotation.w = 1.0
            transforms.append(t)

            # Optical frames (standard camera coordinate convention)
            t_optical = TransformStamped()
            t_optical.header.stamp = timestamp
            t_optical.header.frame_id = f"{self.camera_name}_{frame_name}"
            t_optical.child_frame_id = (
                f'{self.camera_name}_{frame_name.replace("frame", "optical_frame")}'
            )
            # Rotation: X-right, Y-down, Z-forward
            t_optical.transform.rotation.x = -0.5
            t_optical.transform.rotation.y = 0.5
            t_optical.transform.rotation.z = -0.5
            t_optical.transform.rotation.w = 0.5
            transforms.append(t_optical)

        # IMU optical frame
        t_imu = TransformStamped()
        t_imu.header.stamp = timestamp
        t_imu.header.frame_id = f"{self.camera_name}_link"
        t_imu.child_frame_id = f"{self.camera_name}_imu_optical_frame"
        t_imu.transform.rotation.w = 1.0
        transforms.append(t_imu)

        self.static_tf_broadcaster.sendTransform(transforms)

    def _publish_tf(self):
        """Publish dynamic TF transforms.

        Includes the odom->base_link->camera_link chain needed for
        isaac_ros_nvblox and navigation.
        """
        timestamp = self.get_clock().now().to_msg()
        transforms = []

        # Odom to base_link (robot pose in world)
        if self.receiver_config.get("publish_odom", True):
            t_odom = TransformStamped()
            t_odom.header.stamp = timestamp
            t_odom.header.frame_id = self.receiver_config.get("odom_frame", "odom")
            t_odom.child_frame_id = self.receiver_config.get("base_link_frame", "base_link")
            t_odom.transform.translation.x = self.odom_x
            t_odom.transform.translation.y = self.odom_y
            t_odom.transform.translation.z = 0.0

            # Convert theta to quaternion
            quat = self._euler_to_quaternion(0, 0, self.odom_theta)
            t_odom.transform.rotation.x = quat[0]
            t_odom.transform.rotation.y = quat[1]
            t_odom.transform.rotation.z = quat[2]
            t_odom.transform.rotation.w = quat[3]
            transforms.append(t_odom)

        # Base_link to camera_link (camera mounting on robot)
        t_base_cam = TransformStamped()
        t_base_cam.header.stamp = timestamp
        t_base_cam.header.frame_id = self.receiver_config.get("base_link_frame", "base_link")
        t_base_cam.child_frame_id = f"{self.camera_name}_link"
        # Default: camera mounted 0.1m forward, 0.2m up from base
        t_base_cam.transform.translation.x = 0.1
        t_base_cam.transform.translation.y = 0.0
        t_base_cam.transform.translation.z = 0.2
        t_base_cam.transform.rotation.w = 1.0
        transforms.append(t_base_cam)

        self.tf_broadcaster.sendTransform(transforms)

    def _publish_odometry(self):
        """Publish odometry message.

        This provides robot pose and velocity information needed by
        isaac_ros_nvblox for dynamic mapping and navigation.
        """
        odom_msg = Odometry()
        odom_msg.header = Header()
        odom_msg.header.stamp = self.get_clock().now().to_msg()
        odom_msg.header.frame_id = self.receiver_config.get("odom_frame", "odom")
        odom_msg.child_frame_id = self.receiver_config.get("base_link_frame", "base_link")

        # Position
        odom_msg.pose.pose.position.x = self.odom_x
        odom_msg.pose.pose.position.y = self.odom_y
        odom_msg.pose.pose.position.z = 0.0

        # Orientation
        quat = self._euler_to_quaternion(0, 0, self.odom_theta)
        odom_msg.pose.pose.orientation.x = quat[0]
        odom_msg.pose.pose.orientation.y = quat[1]
        odom_msg.pose.pose.orientation.z = quat[2]
        odom_msg.pose.pose.orientation.w = quat[3]

        # Covariance (uncertainty in pose)
        odom_msg.pose.covariance = [
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
        ]

        # Velocity (currently static, can be updated based on IMU integration)
        odom_msg.twist.twist.linear.x = 0.0
        odom_msg.twist.twist.linear.y = 0.0
        odom_msg.twist.twist.angular.z = 0.0

        odom_msg.twist.covariance = [
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.01,
        ]

        self.odom_pub.publish(odom_msg)

    @staticmethod
    def _euler_to_quaternion(roll, pitch, yaw):
        """Convert Euler angles to quaternion."""
        import math

        qx = math.sin(roll / 2) * math.cos(pitch / 2) * math.cos(yaw / 2) - math.cos(
            roll / 2
        ) * math.sin(pitch / 2) * math.sin(yaw / 2)
        qy = math.cos(roll / 2) * math.sin(pitch / 2) * math.cos(yaw / 2) + math.sin(
            roll / 2
        ) * math.cos(pitch / 2) * math.sin(yaw / 2)
        qz = math.cos(roll / 2) * math.cos(pitch / 2) * math.sin(yaw / 2) - math.sin(
            roll / 2
        ) * math.sin(pitch / 2) * math.cos(yaw / 2)
        qw = math.cos(roll / 2) * math.cos(pitch / 2) * math.cos(yaw / 2) + math.sin(
            roll / 2
        ) * math.sin(pitch / 2) * math.sin(yaw / 2)
        return [qx, qy, qz, qw]

    def shutdown(self):
        """Clean shutdown."""
        self.running = False
        self.receiver_thread.join(timeout=2)
        self.socket.close()


class VideoStreamReceiver:
    """Manages video stream reception and ROS2 publishing using tmux.

    This class handles multiple video streams (depth, color, infrared) and
    publishes them to appropriate ROS2 topics using gscam in separate tmux windows.
    """

    def __init__(
        self,
        camera_name: str,
        config_loader: ConfigLoader,
        show_views: bool = False,
        view_scale: float = 0.5,
    ):
        """Initialize the video stream receiver.

        Args:
            camera_name: The name of the camera.
            config_loader: The configuration loader.
            show_views: Whether to show the video streams in a window.
            view_scale: The scale of the video stream window.
        """
        self.camera_name = camera_name
        self.config_loader = config_loader
        self.show_views = show_views
        self.view_scale = view_scale

        self.tmux_manager = TmuxSessionManager()
        self._shutdown_event = threading.Event()

    def start_stream(
        self,
        port: int,
        stream_name: str,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None = None,
    ):
        """Start receiving a video stream and publishing to ROS2."""

        # infra_stereo (Y8I): infra1 and infra2
        if stream_name == "infra_stereo":
            # Y8I splitter - split into infra1 and infra2
            single_width = width // 2

            # infra1
            self._start_single_stream_in_tmux(
                port,
                "infra1",
                encoding,
                single_width,
                height,
                intrinsics,
                is_y8i=True,
                y8i_width=width,
            )
            time.sleep(0.5)

            # infra2
            self._start_single_stream_in_tmux(
                port,
                "infra2",
                encoding,
                single_width,
                height,
                intrinsics,
                is_y8i=True,
                y8i_width=width,
            )
            time.sleep(0.5)
        else:
            self._start_single_stream_in_tmux(
                port, stream_name, encoding, width, height, intrinsics
            )
            time.sleep(0.5)

    def _start_single_stream_in_tmux(
        self,
        port: int,
        stream_name: str,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
        is_y8i: bool = False,
        y8i_width: int = None,
    ):
        """Start a single stream receiver in a tmux window."""

        # Get calibration file
        calib_file = self._get_calibration_file_path(stream_name, width, height)

        if calib_file:
            info_file_url = f"file://{calib_file}"
            tmp_file_path = None
        else:
            print(
                f"  ⚠ No calibration file found for {stream_name} {width}x{height}, using defaults"
            )
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".yaml", prefix=f"{self.camera_name}_{stream_name}_"
            )
            self._create_camera_info_file(tmp_file.name, stream_name, width, height, intrinsics)
            info_file_url = f"file://{tmp_file.name}"
            tmp_file_path = tmp_file.name
            tmp_file.close()

        # Build GStreamer pipeline
        if is_y8i:
            gst_config = self._build_y8i_pipeline(port, stream_name, y8i_width, height)
        else:
            gst_config = self._build_pipeline(port, stream_name, width, height)

        # Determine topics and encoding
        if stream_name == "depth":
            image_topic = f"/{self.camera_name}/depth/image_rect_raw"
            info_topic = f"/{self.camera_name}/depth/camera_info"
            frame_id = f"{self.camera_name}_depth_optical_frame"
            image_encoding = "16UC1"
        elif stream_name == "color":
            image_topic = f"/{self.camera_name}/color/image_raw"
            info_topic = f"/{self.camera_name}/color/camera_info"
            frame_id = f"{self.camera_name}_color_optical_frame"
            image_encoding = "bgr8"
        elif stream_name.startswith("infra"):
            image_topic = f"/{self.camera_name}/{stream_name}/image_rect_raw"
            info_topic = f"/{self.camera_name}/{stream_name}/camera_info"
            frame_id = f"{self.camera_name}_{stream_name}_optical_frame"
            image_encoding = "mono8"
        else:
            return

        # Build gscam command
        gscam_cmd_parts = [
            "ros2 run gscam gscam_node",
            "--ros-args",
            f"-p gscam_config:='{gst_config}'",
            f"-p camera_name:={self.camera_name}_{stream_name}",
            f"-p camera_info_url:={info_file_url}",
            f"-p frame_id:={frame_id}",
            "-p sync_sink:=false",
            f"-p image_encoding:={image_encoding}",
            f"-r camera/image_raw:={image_topic}",
            f"-r camera/camera_info:={info_topic}",
        ]

        gscam_cmd = " ".join(gscam_cmd_parts)

        # Add cleanup of temp file if needed
        if tmp_file_path:
            gscam_cmd = f"trap 'rm -f {tmp_file_path}' EXIT; {gscam_cmd}"

        # Create window name
        window_name = f"{stream_name}_{port}"

        print(f"\n[{stream_name}] Starting on port {port}")
        print(f"  Topic: {image_topic}")
        print(f"  Encoding: {encoding.upper()}")

        # Run in tmux
        self.tmux_manager.create_window(window_name, gscam_cmd)

    def _build_pipeline(self, port: int, stream_name: str, width: int, height: int) -> str:
        """Build GStreamer pipeline for standard streams."""
        if stream_name == "depth":
            # Depth stream: H264 -> GRAY16_LE
            pipeline = (
                f"udpsrc port={port} buffer-size=2097152 "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" '
                f"! rtpjitterbuffer latency=100 drop-on-latency=true "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads=4 "
                f"! queue max-size-buffers=2 leaky=downstream "
                f"! videoconvert n-threads=4 ! video/x-raw,format=GRAY16_LE"
            )
        elif stream_name == "color":
            # Color stream: H264 -> BGR
            pipeline = (
                f"udpsrc port={port} buffer-size=2097152 "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=98" '
                f"! rtpjitterbuffer latency=120 drop-on-latency=true "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads=4 "
                f"! videoconvert n-threads=4 ! video/x-raw,format=BGR"
            )
        elif stream_name.startswith("infra"):
            # Infrared stream: H264 -> GRAY8
            pipeline = (
                f"udpsrc port={port} buffer-size=2097152 "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=97" '
                f"! rtpjitterbuffer latency=100 "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads=4 "
                f"! videoconvert ! video/x-raw,format=GRAY8"
            )
        else:
            return ""

        # Add visualization if enabled
        if self.show_views:
            display_width = int(width * self.view_scale)
            display_height = int(height * self.view_scale)
            pipeline += (
                f" ! tee name=t "
                f"t. ! queue ! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert ! autovideosink sync=false name=viz_{stream_name} "
                f"t. ! queue"
            )

        return pipeline

    def _build_y8i_pipeline(self, port: int, stream_name: str, y8i_width: int, height: int) -> str:
        """Build GStreamer pipeline for Y8I streams (infra1/infra2)."""
        single_width = y8i_width // 2

        pipeline = (
            f"udpsrc port={port} buffer-size=2097152 "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=99" '
            f"! rtpjitterbuffer latency=100 "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads=4 "
            f"! videoconvert ! video/x-raw,format=GRAY8,width={y8i_width},height={height}"
        )

        # Add videocrop to split left/right
        if stream_name == "infra1":
            pipeline += f" ! videocrop right={single_width}"
        else:  # infra2
            pipeline += f" ! videocrop left={single_width}"

        # Add visualization if enabled
        if self.show_views:
            display_width = int(single_width * self.view_scale)
            display_height = int(height * self.view_scale)
            pipeline += (
                f" ! tee name=t "
                f"t. ! queue ! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert ! autovideosink sync=false name=viz_{stream_name} "
                f"t. ! queue"
            )

        return pipeline

    def _get_calibration_file_path(
        self,
        stream_name: str,
        width: int,
        height: int,
    ) -> str | None:
        """Get the path to the calibration file for the given stream and resolution."""
        # Map stream names to file prefixes
        stream_mapping = {
            "depth": "depth",
            "color": "color",
            "infra1": "infrared",
            "infra2": "infrared",
        }

        stream_prefix = stream_mapping.get(stream_name, stream_name)
        resolution = f"{width}x{height}"

        # Search paths for calibration files
        config_paths = [
            Path("src/config"),
            Path(__file__).parent.parent / "config",
            Path.home() / ".config" / "realsense",
        ]

        filename = f"{stream_prefix}_camera_{resolution}.yaml"

        for config_dir in config_paths:
            filepath = config_dir / filename
            if filepath.exists():
                print(f"  ✓ Found calibration file: {filepath}")
                return str(filepath.absolute())

        return None

    def _create_camera_info_file(
        self,
        filepath: str,
        stream_name: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
    ):
        """Create camera_info YAML file."""
        if intrinsics:
            fx, fy = intrinsics.fx, intrinsics.fy
            ppx, ppy = intrinsics.ppx, intrinsics.ppy
            coeffs = intrinsics.distortion
        else:
            # Use defaults from config
            intrinsics = self.config_loader.create_default_intrinsics(width, height)
            fx, fy = intrinsics.fx, intrinsics.fy
            ppx, ppy = intrinsics.ppx, intrinsics.ppy
            coeffs = intrinsics.distortion

        content = f"""image_width: {width}
image_height: {height}
camera_name: {self.camera_name}_{stream_name}
camera_matrix:
  rows: 3
  cols: 3
  data: [{fx}, 0.0, {ppx}, 0.0, {fy}, {ppy}, 0.0, 0.0, 1.0]
distortion_model: plumb_bob
distortion_coefficients:
  rows: 1
  cols: 5
  data: [{coeffs[0]}, {coeffs[1]}, {coeffs[2]}, {coeffs[3]}, {coeffs[4]}]
rectification_matrix:
  rows: 3
  cols: 3
  data: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
projection_matrix:
  rows: 3
  cols: 4
  data: [{fx}, 0.0, {ppx}, 0.0, 0.0, {fy}, {ppy}, 0.0, 0.0, 0.0, 1.0, 0.0]
"""
        with open(filepath, "w") as f:
            f.write(content)

    def wait(self):
        """Wait until shutdown is requested."""
        try:
            self.tmux_manager.attach_info()
            print("\nAll receivers running. Press Ctrl+C to stop.\n")
            self._shutdown_event.wait()
        except KeyboardInterrupt:
            pass

    def stop_all(self):
        """Stop all video receivers and tmux session."""
        self._shutdown_event.set()
        print("\n\nStopping all receivers...")
        try:
            self.tmux_manager.kill_session()
        except Exception as e:
            print(f"tmux cleanup warning: {e}")


def main():
    """Run the RealSense virtual camera receiver."""
    parser = argparse.ArgumentParser(
        description="RealSense Virtual Camera Receiver for isaac_ros_nvblox"
    )
    parser.add_argument("--config", default="src/config/config.yaml", help="Configuration file")
    parser.add_argument(
        "--preset",
        choices=["d435i", "d455", "d415", "l515"],
        help="Use camera preset",
    )
    parser.add_argument("--base-port", type=int, help="Override base port")
    parser.add_argument("--imu-port", type=int, help="Override IMU port")
    parser.add_argument("--camera-name", help="Override camera name")
    parser.add_argument("--local-ip", help="Local IP address to bind to (default: 0.0.0.0)")
    parser.add_argument("--show-views", action="store_true", help="Display received video streams")
    parser.add_argument("--view-scale", type=float, help="Display window scale factor")
    parser.add_argument("--no-imu", action="store_true", help="Disable IMU receiver")
    parser.add_argument("--publish-odom", type=bool, help="Publish odometry")
    parser.add_argument("--width", type=int, help="Image width")
    parser.add_argument("--height", type=int, help="Image height")

    args = parser.parse_args()

    # Load configuration
    config_loader = ConfigLoader(args.config)

    # Get configurations
    network_cfg = config_loader.get_network_config(args)
    camera_cfg = config_loader.get_camera_config(args)
    receiver_cfg = config_loader.get_receiver_config(args)
    config_loader.get_imu_config(args)

    # Resolution
    width = args.width or 640
    height = args.height or 480

    # Handle presets
    if args.preset:
        preset = config_loader.get_preset(args.preset)
        if preset:
            streams = preset["streams"]
            ports: list = []
            stream_names: list = []
            encodings: list = []
            widths: list = []
            heights: list = []

            for s in streams:
                port = network_cfg["base_port"] + s["port_offset"]
                ports.append(port)
                stream_names.append(s["name"])
                encodings.append(s["encoding"])

                if s["name"] == "infra_stereo":
                    widths.append(width * 2)  # Y8I is double width
                else:
                    widths.append(width)
                heights.append(height)

            print(f"Using {args.preset.upper()} preset")
        else:
            print(f"Error: Preset {args.preset} not found")
            sys.exit(1)
    else:
        print("Error: --preset required (d435i, d455, d415, l515)")
        sys.exit(1)

    print(f"\n{'='*70}")
    print("VIRTUAL REALSENSE CAMERA RECEIVER")
    print(f"{'='*70}")
    print(f"Camera Name: {camera_cfg['camera_name']}")
    print(f"IMU Port: {network_cfg['imu_port']}")
    print(f"Local IP: {receiver_cfg['local_ip']}")
    print("\nVideo Streams:")
    for port, stream, enc in zip(ports, stream_names, encodings, strict=False):
        print(f"  {stream:10s} - Port {port} ({enc.upper()})")
    print(f"{'='*70}\n")

    # Initialize ROS2
    rclpy.init()

    # Create virtual camera node
    virtual_camera = VirtualRealSenseNode(
        camera_name=camera_cfg["camera_name"],
        imu_port=network_cfg["imu_port"],
        config_loader=config_loader,
        receiver_config=receiver_cfg,
    )

    # Spin node in background
    ros_thread = threading.Thread(target=lambda: rclpy.spin(virtual_camera), daemon=True)
    ros_thread.start()
    time.sleep(1)

    # Start video receivers in tmux
    video_receiver = VideoStreamReceiver(
        camera_name=camera_cfg["camera_name"],
        config_loader=config_loader,
        show_views=receiver_cfg.get("show_views", False),
        view_scale=receiver_cfg.get("view_scale", 0.5),
    )

    for port, stream, encoding, stream_width, stream_height in zip(
        ports, stream_names, encodings, widths, heights, strict=False
    ):
        intrinsics = config_loader.create_default_intrinsics(
            stream_width if stream != "infra_stereo" else stream_width // 2, stream_height
        )
        video_receiver.start_stream(port, stream, encoding, stream_width, stream_height, intrinsics)

    time.sleep(1)
    print(f"\n{'='*70}")
    print("ALL RECEIVERS STARTED")
    print(f"{'='*70}")

    print("\nPublishing ROS2 topics:")
    print(f"  /{camera_cfg['camera_name']}/depth/image_rect_raw")
    print(f"  /{camera_cfg['camera_name']}/color/image_raw")

    # Check if infra_stereo is present
    if "infra_stereo" in stream_names:
        print(f"  /{camera_cfg['camera_name']}/infra1/image_rect_raw (from Y8I left)")
        print(f"  /{camera_cfg['camera_name']}/infra2/image_rect_raw (from Y8I right)")
    else:
        print(f"  /{camera_cfg['camera_name']}/infra1/image_rect_raw")
        if "infra2" in stream_names:
            print(f"  /{camera_cfg['camera_name']}/infra2/image_rect_raw")

    print(f"  /{camera_cfg['camera_name']}/imu")
    if receiver_cfg.get("publish_odom"):
        print(f"  /{camera_cfg['camera_name']}/odom")
    print(f"\nTF tree: odom → base_link → {camera_cfg['camera_name']}_link → sensor frames")

    # Wait for shutdown
    try:
        video_receiver.wait()
    except KeyboardInterrupt:
        print("\n\nShutting down...")

    # Cleanup
    video_receiver.stop_all()
    virtual_camera.shutdown()
    virtual_camera.destroy_node()
    rclpy.shutdown()
    print("✓ Shutdown complete")


if __name__ == "__main__":
    main()
