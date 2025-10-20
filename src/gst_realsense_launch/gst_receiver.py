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

from utils.logger import LOGGER

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None
    LOGGER.info("Warning: OpenCV not available. --show-views will be disabled.")

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
    LOGGER.info("Error: ROS2 not found. Source your ROS2 installation:")
    LOGGER.info("  source /opt/ros/humble/setup.bash")
    sys.exit(1)

from gst_realsense_launch.rs_common import CameraIntrinsics, ConfigLoader
from gst_realsense_launch.rs_core import StreamStrategyFactory


class TmuxSessionManager:
    """Manages tmux session for multiple GStreamer receiver pipelines."""

    def __init__(self, session_name: str = "realsense_receiver"):
        self.session_name = session_name
        self.window_count = 0
        self._check_tmux()
        self._cleanup_old_session()
        self._create_session()

    def _check_tmux(self):
        """Check if tmux is available."""
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("tmux is not installed. Please install tmux: sudo apt install tmux")

    def _cleanup_old_session(self):
        """Kill old session if it exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
            LOGGER.info(f"✓ Cleaned up old tmux session '{self.session_name}'")
            time.sleep(0.5)

    def _create_session(self):
        """Create tmux session if it doesn't exist."""
        try:
            # Create new detached session with a dummy command
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name],
                check=True,
                capture_output=True,
            )
            LOGGER.info(f"✓ Created tmux session '{self.session_name}'")
        except subprocess.CalledProcessError as e:
            LOGGER.info(f"Warning: Could not create tmux session: {e}")
            raise

    def create_window(self, window_name: str, command: str):
        """Create a new tmux window and run command in it.

        Args:
            window_name: Name for the tmux window
            command: Command to execute in the window
        """
        try:
            # Create new window (tmux will auto-assign window index)
            result = subprocess.run(
                [
                    "tmux",
                    "new-window",
                    "-t",
                    self.session_name,
                    "-n",
                    window_name,
                ],
                check=True,
                capture_output=True,
                text=True,
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
                capture_output=True,
            )

            self.window_count += 1
            LOGGER.info(f"  ✓ Created window '{window_name}' in tmux")

        except subprocess.CalledProcessError as e:
            LOGGER.info(f"  ✗ Failed to create window '{window_name}': {e}")
            if e.stderr:
                LOGGER.info(f"     Error output: {e.stderr}")
            raise

    def attach_info(self):
        """Display instructions for attaching to the tmux session."""
        LOGGER.info(f"\nTo view the streams, attach to tmux session:")
        LOGGER.info(f"  tmux attach -t {self.session_name}")
        LOGGER.info(f"\nTmux navigation:")
        LOGGER.info(f"  Ctrl+b n : next window")
        LOGGER.info(f"  Ctrl+b p : previous window")
        LOGGER.info(f"  Ctrl+b [0-9] : select window by number")
        LOGGER.info(f"  Ctrl+b d : detach from session")
        LOGGER.info(f"  Ctrl+b & : kill current window")

    def kill_session(self):
        """Kill the entire tmux session and ensure all processes are terminated."""
        if not self._session_exists():
            LOGGER.info(f"Tmux session '{self.session_name}' already terminated")
            return

        try:
            # Step 1: List all windows and their panes
            result = subprocess.run(
                ["tmux", "list-panes", "-t", self.session_name, "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,  # ✅ 添加這個確保返回 str
                timeout=5,
            )

            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                LOGGER.info(f"Found {len(pids)} processes in tmux session")

                # Step 2: Send SIGTERM to all processes
                for pid in pids:
                    if pid and pid.isdigit():
                        try:
                            # Send SIGTERM first (graceful)
                            subprocess.run(["kill", "-TERM", pid], timeout=2, check=False)
                            LOGGER.debug(f"  Sent SIGTERM to PID {pid}")
                        except Exception as e:
                            LOGGER.debug(f"  Could not terminate PID {pid}: {e}")

                # Step 3: Wait a bit for graceful shutdown
                time.sleep(2)

                # Step 4: Force kill any remaining processes
                for pid in pids:
                    if pid and pid.isdigit():
                        try:
                            # Check if process still exists
                            check = subprocess.run(
                                ["ps", "-p", pid],
                                capture_output=True,
                                timeout=1,
                                check=False,
                            )
                            if check.returncode == 0:
                                # Process still alive, force kill
                                subprocess.run(["kill", "-KILL", pid], timeout=1, check=False)
                                LOGGER.debug(f"  Force killed PID {pid}")
                        except Exception as e:
                            LOGGER.debug(f"  Could not check/kill PID {pid}: {e}")

            # Step 5: Kill the tmux session itself
            result = subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                LOGGER.info(f"✓ Killed tmux session '{self.session_name}'")
            else:
                LOGGER.warning(f"Could not kill tmux session (may already be gone)")

            # Step 6: Verify session is gone
            time.sleep(0.5)
            if not self._session_exists():
                LOGGER.info("✓ Tmux session cleanup verified")
            else:
                LOGGER.warning("⚠ Tmux session may still exist")

        except subprocess.TimeoutExpired:
            LOGGER.error("Timeout while trying to kill tmux session")
            # Last resort: force kill tmux server
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", f"tmux.*{self.session_name}"],
                    timeout=2,
                    check=False,
                )
            except Exception as e:
                LOGGER.error(f"Could not force kill tmux: {e}")
        except Exception as e:
            LOGGER.error(f"Error during tmux cleanup: {e}")

    def _session_exists(self) -> bool:
        """Check if the tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
        )
        return result.returncode == 0


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
        """Clean shutdown with proper resource cleanup."""
        LOGGER.info("Shutting down VirtualRealSenseNode...")

        # Step 1: Stop receiver thread
        self.running = False
        if self.receiver_thread and self.receiver_thread.is_alive():
            LOGGER.info("  Stopping receiver thread...")
            self.receiver_thread.join(timeout=3)
            if self.receiver_thread.is_alive():
                LOGGER.warning("  Receiver thread did not stop gracefully")

        # Step 2: Stop timers
        try:
            if hasattr(self, "tf_timer"):
                self.tf_timer.cancel()
            if hasattr(self, "odom_timer"):
                self.odom_timer.cancel()
            LOGGER.info("  ✓ Timers stopped")
        except Exception as e:
            LOGGER.debug(f"  Error stopping timers: {e}")

        # Step 3: Close socket
        try:
            if hasattr(self, "socket"):
                self.socket.close()
            LOGGER.info("  ✓ Socket closed")
        except Exception as e:
            LOGGER.debug(f"  Error closing socket: {e}")

        # Step 4: Destroy publishers
        try:
            if hasattr(self, "imu_pub"):
                self.destroy_publisher(self.imu_pub)
            if hasattr(self, "odom_pub"):
                self.destroy_publisher(self.odom_pub)
            LOGGER.info("  ✓ Publishers destroyed")
        except Exception as e:
            LOGGER.debug(f"  Error destroying publishers: {e}")

        LOGGER.info("✓ VirtualRealSenseNode shutdown complete")


class VideoStreamReceiver:
    """Manages video stream reception and ROS2 publishing using tmux."""

    def __init__(
        self,
        camera_name: str,
        config_loader: ConfigLoader,
        show_views: bool = False,
        view_scale: float = 0.5,
    ):
        """Initialize the video stream receiver."""
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

            # infra1 - gscam for ROS2
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
            # infra1 - optional visualization
            if self.show_views:
                self._start_visualization_in_tmux(
                    port, "infra1", single_width, height, is_y8i=True, y8i_width=width
                )
            time.sleep(0.5)

            # infra2 - gscam for ROS2
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
            # infra2 - optional visualization
            if self.show_views:
                self._start_visualization_in_tmux(
                    port, "infra2", single_width, height, is_y8i=True, y8i_width=width
                )
            time.sleep(0.5)
        else:
            # gscam for ROS2
            self._start_single_stream_in_tmux(
                port, stream_name, encoding, width, height, intrinsics
            )
            # Optional visualization
            if self.show_views:
                self._start_visualization_in_tmux(port, stream_name, width, height)
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
            LOGGER.info(
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

        # Get image encoding from config
        image_encodings = self.config_loader.get("receiver.image_encoding", {})

        # Determine topics and encoding
        if stream_name == "depth":
            image_topic = f"/{self.camera_name}/depth/image_rect_raw"
            info_topic = f"/{self.camera_name}/depth/camera_info"
            frame_id = f"{self.camera_name}_depth_optical_frame"
            image_encoding = None
        elif stream_name == "color":
            image_topic = f"/{self.camera_name}/color/image_raw"
            info_topic = f"/{self.camera_name}/color/camera_info"
            frame_id = f"{self.camera_name}_color_optical_frame"
            image_encoding = image_encodings.get("color", "rgb8")
        elif stream_name.startswith("infra"):
            image_topic = f"/{self.camera_name}/{stream_name}/image_rect_raw"
            info_topic = f"/{self.camera_name}/{stream_name}/camera_info"
            frame_id = f"{self.camera_name}_{stream_name}_optical_frame"
            image_encoding = image_encodings.get("infra", "mono8")
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
        ]

        # 只有在 image_encoding 不是 None 時才添加
        if image_encoding:
            gscam_cmd_parts.append(f"-p image_encoding:={image_encoding}")

        gscam_cmd_parts.extend(
            [
                f"-r camera/image_raw:={image_topic}",
                f"-r camera/camera_info:={info_topic}",
            ]
        )

        gscam_cmd = " ".join(gscam_cmd_parts)

        # Add cleanup of temp file if needed
        if tmp_file_path:
            gscam_cmd = f"trap 'rm -f {tmp_file_path}' EXIT; {gscam_cmd}"

        # Create window name
        window_name = f"{stream_name}_{port}"

        LOGGER.info(f"\n[{stream_name}] Starting on port {port}")
        LOGGER.info(f"  Topic: {image_topic}")
        LOGGER.info(f"  Encoding: {image_encoding if image_encoding else 'auto-detect (mono16)'}")
        LOGGER.info(f"  GStreamer Config: {gst_config[:100]}...")

        # Run in tmux
        self.tmux_manager.create_window(window_name, gscam_cmd)

        # 如果是深度流且啟用了轉換，啟動 depth_image_proc 節點
        if stream_name == "depth":
            self._start_depth_conversion_node(port, image_topic, info_topic)

    def _start_depth_conversion_node(self, port: int, depth_topic: str, info_topic: str):
        """Start depth_image_proc convert_metric node for float32 conversion."""

        depth_config = self.config_loader.get("receiver.depth_conversion", {})

        if not depth_config.get("enabled", True):
            LOGGER.info("  Depth conversion disabled in config")
            return

        output_topic = depth_config.get("output_topic", "depth/image")
        output_full_topic = f"/{self.camera_name}/{output_topic}"

        # Build depth conversion command
        convert_cmd_parts = [
            "ros2 run depth_image_proc convert_metric_node",
            "--ros-args",
            f"-r image_raw:={depth_topic}",
            f"-r camera_info:={info_topic}",
            f"-r image:={output_full_topic}",
        ]

        convert_cmd = " ".join(convert_cmd_parts)
        window_name = f"depth_convert_{port}"

        LOGGER.info(f"  [CONVERT] Starting depth to float32 conversion")
        LOGGER.info(f"    Input: {depth_topic} (mono16)")
        LOGGER.info(f"    Output: {output_full_topic} (32FC1)")

        # 延遲啟動，確保 gscam 先啟動
        time.sleep(0.5)
        self.tmux_manager.create_window(window_name, convert_cmd)

    def _build_pipeline(self, port: int, stream_name: str, width: int, height: int) -> str:
        """Build GStreamer pipeline for standard streams (ROS2 only, no visualization)."""

        # Get streaming parameters from config
        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")

        # Get payload types from config
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        # Get format conversion settings from config
        gst_formats = self.config_loader.get("receiver.gstreamer_format", {})

        if stream_name == "depth":
            # Depth stream: H264 -> GRAY16_LE
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("depth_h264", 96)
            output_format = gst_formats.get("depth", "GRAY16_LE")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        elif stream_name == "color":
            # Color stream: H264 -> RGB
            latency = self.config_loader.get("streaming.jitter_buffer.color.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.color.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("color_h264", 98)
            output_format = gst_formats.get("color", "RGB")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        elif stream_name.startswith("infra"):
            # Infrared stream: H264 -> GRAY8
            latency = self.config_loader.get("streaming.jitter_buffer.infra.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.infra.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("ir_h264", 97)
            output_format = gst_formats.get("infra", "GRAY8")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        else:
            return ""

        return pipeline

    def _build_y8i_pipeline(self, port: int, stream_name: str, y8i_width: int, height: int) -> str:
        """Build GStreamer pipeline for Y8I streams (ROS2 only, no visualization)."""
        single_width = y8i_width // 2

        # Get streaming parameters from config
        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
        latency = self.config_loader.get("streaming.jitter_buffer.infra_stereo.latency", 200)
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.infra_stereo.drop_on_latency", True
        )
        drop_str = "true" if drop_on_latency else "false"
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")

        # Get payload type from config
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        payload = payload_types.get("infra_stereo_h264", 99)

        pipeline = (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
            f"! videoconvert n-threads={n_threads} ! video/x-raw,format=GRAY8,width={y8i_width},height={height}"
        )

        # Add videocrop to split left/right
        if stream_name == "infra1":
            pipeline += f" ! videocrop right={single_width}"
        else:  # infra2
            pipeline += f" ! videocrop left={single_width}"

        return pipeline

    def _start_visualization_in_tmux(
        self,
        port: int,
        stream_name: str,
        width: int,
        height: int,
        is_y8i: bool = False,
        y8i_width: int = None,
    ):
        """Start a separate visualization window for a stream."""
        display_width = int(width * self.view_scale)
        display_height = int(height * self.view_scale)

        # Build visualization pipeline (independent of gscam)
        if is_y8i:
            viz_pipeline = self._build_visualization_pipeline_y8i(
                port, stream_name, y8i_width, height, display_width, display_height
            )
        else:
            viz_pipeline = self._build_visualization_pipeline(
                port, stream_name, width, height, display_width, display_height
            )

        window_name = f"viz_{stream_name}_{port}"

        # Create gst-launch command
        gst_cmd = f"gst-launch-1.0 -v {viz_pipeline}"

        LOGGER.info(f"  [VIZ] Starting visualization for {stream_name}")
        self.tmux_manager.create_window(window_name, gst_cmd)

    def _build_visualization_pipeline(
        self,
        port: int,
        stream_name: str,
        width: int,
        height: int,
        display_width: int,
        display_height: int,
    ) -> str:
        """Build independent visualization pipeline."""

        # Get streaming parameters from config
        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        if stream_name == "depth":
            # Depth visualization with normalization for better viewing
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("depth_h264", 96)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        elif stream_name == "color":
            # Color visualization
            latency = self.config_loader.get("streaming.jitter_buffer.color.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.color.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("color_h264", 98)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        elif stream_name.startswith("infra"):
            # Infrared visualization
            latency = self.config_loader.get("streaming.jitter_buffer.infra.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.infra.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("ir_h264", 97)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        else:
            return ""

        return pipeline

    def _build_visualization_pipeline_y8i(
        self,
        port: int,
        stream_name: str,
        y8i_width: int,
        height: int,
        display_width: int,
        display_height: int,
    ) -> str:
        """Build independent visualization pipeline for Y8I streams."""
        single_width = y8i_width // 2

        # Get streaming parameters from config
        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
        latency = self.config_loader.get("streaming.jitter_buffer.infra_stereo.latency", 200)
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.infra_stereo.drop_on_latency", True
        )
        drop_str = "true" if drop_on_latency else "false"
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        payload = payload_types.get("infra_stereo_h264", 99)

        pipeline = (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
            f"! videoconvert ! video/x-raw,format=GRAY8,width={y8i_width},height={height}"
        )

        # Add videocrop to split left/right
        if stream_name == "infra1":
            pipeline += f" ! videocrop right={single_width}"
        else:  # infra2
            pipeline += f" ! videocrop left={single_width}"

        # Add scaling and display
        pipeline += (
            f" ! videoscale ! video/x-raw,width={display_width},height={display_height} "
            f"! videoconvert "
            f"! autovideosink sync=false"
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
                LOGGER.info(f"  ✓ Found calibration file: {filepath}")
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
            LOGGER.info("\nAll receivers running. Press Ctrl+C to stop.\n")
            self._shutdown_event.wait()
        except KeyboardInterrupt:
            pass

    def stop_all(self):
        """Stop all video receivers and tmux session with thorough cleanup."""
        self._shutdown_event.set()
        LOGGER.info("\n\n" + "=" * 70)
        LOGGER.info("INITIATING SHUTDOWN - Stopping all receivers")
        LOGGER.info("=" * 70)

        cleanup_successful = True

        try:
            # Step 1: Kill tmux session and all child processes
            if self.tmux_manager:
                LOGGER.info("\n[1/3] Stopping GStreamer pipelines...")
                self.tmux_manager.kill_session()
                time.sleep(1)

            # Step 2: Clean up any orphaned gscam processes
            LOGGER.info("\n[2/3] Checking for orphaned processes...")
            self._cleanup_orphaned_processes()

            # Step 3: Release any locked resources
            LOGGER.info("\n[3/3] Releasing resources...")
            self._release_resources()

        except Exception as e:
            LOGGER.error(f"Error during cleanup: {e}")
            cleanup_successful = False

        LOGGER.info("\n" + "=" * 70)
        if cleanup_successful:
            LOGGER.info("✓ SHUTDOWN COMPLETE - All resources released")
        else:
            LOGGER.warning("⚠ SHUTDOWN COMPLETED WITH WARNINGS")
            LOGGER.info("  Run this command to force cleanup:")
            LOGGER.info(f"    pkill -9 -f 'gscam|depth_image_proc|{self.camera_name}'")
        LOGGER.info("=" * 70 + "\n")

    def _cleanup_orphaned_processes(self):
        """Clean up any orphaned gscam or depth_image_proc processes."""
        process_patterns = [
            f"gscam.*{self.camera_name}",
            f"depth_image_proc.*{self.camera_name}",
            "convert_metric_node",
        ]

        for pattern in process_patterns:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )

                if result.returncode == 0 and result.stdout:
                    pids = result.stdout.strip().split("\n")
                    for pid in pids:
                        if pid and pid.isdigit():
                            try:
                                subprocess.run(
                                    ["kill", "-TERM", pid],
                                    timeout=1,
                                    check=False,
                                )
                                LOGGER.info(f"  Terminated orphaned process (PID {pid})")
                                time.sleep(0.5)

                                check = subprocess.run(
                                    ["ps", "-p", pid],
                                    capture_output=True,
                                    timeout=1,
                                    check=False,
                                )
                                if check.returncode == 0:
                                    subprocess.run(
                                        ["kill", "-KILL", pid],
                                        timeout=1,
                                        check=False,
                                    )
                                    LOGGER.info(f"  Force killed process (PID {pid})")
                            except Exception as e:
                                LOGGER.debug(f"  Could not kill PID {pid}: {e}")
            except Exception as e:
                LOGGER.debug(f"  Error checking pattern '{pattern}': {e}")

    def _release_resources(self):
        """Release any resources that might be locked."""
        try:
            temp_dir = Path(tempfile.gettempdir())
            temp_files = temp_dir.glob(f"{self.camera_name}_*_*.yaml")

            for temp_file in temp_files:
                try:
                    temp_file.unlink()
                    LOGGER.debug(f"  Removed temp file: {temp_file}")
                except Exception as e:
                    LOGGER.debug(f"  Could not remove {temp_file}: {e}")
        except Exception as e:
            LOGGER.debug(f"  Error cleaning temp files: {e}")

        LOGGER.info("  ✓ Resources released")


def check_and_cleanup_existing_resources(camera_name: str):
    """Check for and clean up any existing resources before starting."""
    LOGGER.info("Checking for existing resources...")

    issues_found = False

    # Check for existing tmux session
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["tmux", "has-session", "-t", "realsense_receiver"], capture_output=True, text=True
    )
    if result.returncode == 0:
        LOGGER.warning("  ⚠ Found existing tmux session 'realsense_receiver'")
        issues_found = True

        # Ask user if they want to clean up
        try:
            response = input("    Clean up existing session? (y/n): ").lower()
            if response == "y":
                subprocess.run(["tmux", "kill-session", "-t", "realsense_receiver"])
                LOGGER.info("    ✓ Cleaned up existing session")
                time.sleep(1)
        except:
            LOGGER.info("    Automatically cleaning up...")
            subprocess.run(["tmux", "kill-session", "-t", "realsense_receiver"])
            time.sleep(1)

    # Check for orphaned processes
    patterns = [
        f"gscam.*{camera_name}",
        "depth_image_proc",
        "convert_metric_node",
    ]

    for pattern in patterns:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            LOGGER.warning(f"  ⚠ Found {len(pids)} orphaned processes matching '{pattern}'")
            issues_found = True

            for pid in pids:
                if pid and pid.isdigit():
                    try:
                        subprocess.run(["kill", "-TERM", pid], timeout=1)
                        LOGGER.info(f"    ✓ Terminated PID {pid}")
                    except:
                        pass

    if issues_found:
        LOGGER.info("  Waiting for cleanup to complete...")
        time.sleep(2)
        LOGGER.info("✓ Cleanup complete")
    else:
        LOGGER.info("✓ No existing resources found")

    return True


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

    try:

        # Load configuration
        config_loader = ConfigLoader(args.config)

        # Get configurations
        network_cfg = config_loader.get_network_config(args)
        camera_cfg = config_loader.get_camera_config(args)
        receiver_cfg = config_loader.get_receiver_config(args)
        config_loader.get_imu_config(args)

        check_and_cleanup_existing_resources(camera_cfg["camera_name"])

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

                LOGGER.info(f"Using {args.preset.upper()} preset")
            else:
                LOGGER.info(f"Error: Preset {args.preset} not found")
                sys.exit(1)
        else:
            LOGGER.info("Error: --preset required (d435i, d455, d415, l515)")
            sys.exit(1)

        LOGGER.info(f"\n{'='*70}")
        LOGGER.info("VIRTUAL REALSENSE CAMERA RECEIVER")
        LOGGER.info(f"{'='*70}")
        LOGGER.info(f"Camera Name: {camera_cfg['camera_name']}")
        LOGGER.info(f"IMU Port: {network_cfg['imu_port']}")
        LOGGER.info(f"Local IP: {receiver_cfg['local_ip']}")
        LOGGER.info("\nVideo Streams:")
        for port, stream, enc in zip(ports, stream_names, encodings, strict=False):
            LOGGER.info(f"  {stream:10s} - Port {port} ({enc.upper()})")
        LOGGER.info(f"{'='*70}\n")

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
            video_receiver.start_stream(
                port, stream, encoding, stream_width, stream_height, intrinsics
            )

        time.sleep(1)
        LOGGER.info(f"\n{'='*70}")
        LOGGER.info("ALL RECEIVERS STARTED")
        LOGGER.info(f"{'='*70}")

        LOGGER.info("\nPublishing ROS2 topics:")
        LOGGER.info(f"  /{camera_cfg['camera_name']}/depth/image_rect_raw")
        LOGGER.info(f"  /{camera_cfg['camera_name']}/color/image_raw")

        # Check if infra_stereo is present
        if "infra_stereo" in stream_names:
            LOGGER.info(f"  /{camera_cfg['camera_name']}/infra1/image_rect_raw (from Y8I left)")
            LOGGER.info(f"  /{camera_cfg['camera_name']}/infra2/image_rect_raw (from Y8I right)")
        else:
            LOGGER.info(f"  /{camera_cfg['camera_name']}/infra1/image_rect_raw")
            if "infra2" in stream_names:
                LOGGER.info(f"  /{camera_cfg['camera_name']}/infra2/image_rect_raw")

        LOGGER.info(f"  /{camera_cfg['camera_name']}/imu")
        if receiver_cfg.get("publish_odom"):
            LOGGER.info(f"  /{camera_cfg['camera_name']}/odom")
        LOGGER.info(
            f"\nTF tree: odom → base_link → {camera_cfg['camera_name']}_link → sensor frames"
        )

        # Wait for shutdown
        try:
            video_receiver.wait()
        except KeyboardInterrupt:
            LOGGER.info("\n\nShutting down...")
    except Exception as e:
        LOGGER.error(f"\n❌ Fatal error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        # Ensure cleanup happens no matter what
        LOGGER.info("\nStarting cleanup sequence...")

        # Cleanup video receiver (this includes tmux)
        if video_receiver:
            try:
                video_receiver.stop_all()
            except Exception as e:
                LOGGER.error(f"Error stopping video receiver: {e}")

        # Cleanup virtual camera node
        if virtual_camera:
            try:
                virtual_camera.shutdown()
                virtual_camera.destroy_node()
            except Exception as e:
                LOGGER.error(f"Error shutting down virtual camera: {e}")

        # Shutdown ROS2
        try:
            if rclpy.ok():
                rclpy.shutdown()
                LOGGER.info("✓ ROS2 shutdown complete")
        except Exception as e:
            LOGGER.error(f"Error shutting down ROS2: {e}")

        # Wait for ROS thread to finish
        if ros_thread and ros_thread.is_alive():
            ros_thread.join(timeout=2)

        LOGGER.info("\n" + "=" * 70)
        LOGGER.info("✓ ALL CLEANUP COMPLETE")
        LOGGER.info("=" * 70 + "\n")


if __name__ == "__main__":
    import signal

    def signal_handler(signum, frame):
        LOGGER.info(f"\n⚠ Received signal {signum}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
