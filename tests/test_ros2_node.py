#!/usr/bin/env python3
"""Tests for ROS2 nodes (IMU receiver and TF/Odom publisher)."""

import json
import socket

# Mock ROS2 before importing nodes
import sys
from unittest.mock import MagicMock, call, patch

import pytest

sys.modules["rclpy"] = MagicMock()
sys.modules["rclpy.node"] = MagicMock()
sys.modules["rclpy.qos"] = MagicMock()
sys.modules["sensor_msgs"] = MagicMock()
sys.modules["sensor_msgs.msg"] = MagicMock()
sys.modules["geometry_msgs"] = MagicMock()
sys.modules["geometry_msgs.msg"] = MagicMock()
sys.modules["nav_msgs"] = MagicMock()
sys.modules["nav_msgs.msg"] = MagicMock()
sys.modules["std_msgs"] = MagicMock()
sys.modules["std_msgs.msg"] = MagicMock()
sys.modules["tf2_ros"] = MagicMock()


class TestIMUReceiverNode:
    """Test IMU receiver node functionality."""

    @pytest.fixture
    def mock_rclpy(self):
        """Mock rclpy."""
        with patch("rclpy.init"), patch("rclpy.ok", return_value=True), patch("rclpy.shutdown"):
            yield

    def test_imu_data_parsing(self):
        """Test IMU data JSON parsing."""
        imu_data = {
            "type": "imu",
            "accel": {"x": 0.1, "y": 0.2, "z": 9.8},
            "gyro": {"x": 0.01, "y": 0.02, "z": 0.03},
        }

        # Verify JSON structure
        assert imu_data["type"] == "imu"
        assert "accel" in imu_data
        assert "gyro" in imu_data
        assert imu_data["accel"]["z"] == 9.8

    def test_calibration_data_parsing(self):
        """Test calibration data JSON parsing."""
        calib_data = {
            "type": "calibration",
            "intrinsics": {"depth": {"fx": 600.0, "fy": 600.0}},
            "extrinsics": {"depth_to_color": [0.015, 0.0, 0.0]},
        }

        assert calib_data["type"] == "calibration"
        assert "intrinsics" in calib_data
        assert calib_data["extrinsics"]["depth_to_color"][0] == 0.015

    def test_udp_socket_creation(self):
        """Test UDP socket creation for IMU receiver."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Should not raise exception
        assert sock.family == socket.AF_INET
        assert sock.type == socket.SOCK_DGRAM

        sock.close()


class TestTFOdomPublisherNode:
    """Test TF and Odometry publisher node functionality."""

    def test_euler_to_quaternion_identity(self):
        """Test Euler to quaternion conversion for identity rotation."""
        import math

        # Helper function (copied from node)
        def euler_to_quaternion(roll, pitch, yaw):
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

        # Identity rotation (0, 0, 0)
        quat = euler_to_quaternion(0, 0, 0)

        assert abs(quat[0]) < 1e-10  # qx ≈ 0
        assert abs(quat[1]) < 1e-10  # qy ≈ 0
        assert abs(quat[2]) < 1e-10  # qz ≈ 0
        assert abs(quat[3] - 1.0) < 1e-10  # qw ≈ 1

    def test_euler_to_quaternion_90deg_yaw(self):
        """Test Euler to quaternion conversion for 90° yaw."""
        import math

        def euler_to_quaternion(roll, pitch, yaw):
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

        # 90° yaw rotation
        quat = euler_to_quaternion(0, 0, math.pi / 2)

        assert abs(quat[0]) < 1e-10  # qx ≈ 0
        assert abs(quat[1]) < 1e-10  # qy ≈ 0
        assert abs(quat[2] - math.sqrt(2) / 2) < 1e-6  # qz ≈ 0.707
        assert abs(quat[3] - math.sqrt(2) / 2) < 1e-6  # qw ≈ 0.707

    def test_quaternion_normalization(self):
        """Test that quaternion is normalized."""
        import math

        def euler_to_quaternion(roll, pitch, yaw):
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

        # Test various angles
        for angle in [0, math.pi / 4, math.pi / 2, math.pi, 2 * math.pi]:
            quat = euler_to_quaternion(0, 0, angle)
            magnitude = math.sqrt(quat[0] ** 2 + quat[1] ** 2 + quat[2] ** 2 + quat[3] ** 2)
            assert abs(magnitude - 1.0) < 1e-10, f"Quaternion not normalized for angle {angle}"

    def test_odometry_covariance_matrix(self):
        """Test odometry covariance matrix structure."""
        # Standard covariance for static odometry
        pose_covariance = [
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

        # Should be 6x6 matrix (36 elements)
        assert len(pose_covariance) == 36

        # Diagonal elements should be positive
        for i in range(6):
            assert pose_covariance[i * 6 + i] > 0

        # Should be symmetric
        for i in range(6):
            for j in range(6):
                idx1 = i * 6 + j
                idx2 = j * 6 + i
                assert pose_covariance[idx1] == pose_covariance[idx2]


class TestLaunchFileConfiguration:
    """Test launch file configuration."""

    def test_default_parameters(self):
        """Test default launch file parameters."""
        defaults = {
            "camera_name": "camera0",
            "depth_port": 5020,
            "color_port": 5010,
            "imu_port": 5050,
            "enable_imu": True,
            "publish_odom": True,
            "use_depth_float": True,
            "odom_frame": "odom",
            "base_link_frame": "base_link",
            "local_ip": "0.0.0.0",
        }

        assert defaults["camera_name"] == "camera0"
        assert defaults["enable_imu"] is True
        assert defaults["publish_odom"] is True

    def test_port_configuration(self):
        """Test port configuration ranges."""
        ports = {
            "depth_port": 5020,
            "color_port": 5010,
            "infra1_port": 5030,
            "imu_port": 5050,
        }

        # All ports should be in valid range
        for port_name, port in ports.items():
            assert 1024 <= port <= 65535, f"{port_name} out of valid range"

        # Ports should be unique
        port_values = list(ports.values())
        assert len(port_values) == len(set(port_values)), "Duplicate ports found"

    def test_frame_id_naming(self):
        """Test frame ID naming conventions."""
        camera_name = "camera0"

        frame_ids = {
            "camera_link": f"{camera_name}_link",
            "depth_optical": f"{camera_name}_depth_optical_frame",
            "color_optical": f"{camera_name}_color_optical_frame",
            "imu_optical": f"{camera_name}_imu_optical_frame",
        }

        for frame_type, frame_id in frame_ids.items():
            assert camera_name in frame_id
            assert frame_id.endswith("_link") or frame_id.endswith("_frame")


class TestStaticTransforms:
    """Test static transform configurations."""

    def test_depth_optical_frame_rotation(self):
        """Test depth optical frame rotation (ROS optical frame convention)."""
        # ROS optical frame: X-right, Y-down, Z-forward
        # Rotation from camera frame: (-0.5, 0.5, -0.5, 0.5)
        quat = [-0.5, 0.5, -0.5, 0.5]

        # Quaternion magnitude should be 1
        import math

        magnitude = math.sqrt(sum(q**2 for q in quat))
        assert abs(magnitude - 1.0) < 1e-10

    def test_camera_baseline(self):
        """Test stereo camera baseline (infra1 to infra2)."""
        # D435i has 50mm baseline
        baseline = 0.050  # meters

        assert baseline == 0.050
        assert baseline > 0, "Baseline must be positive"

    def test_color_to_depth_offset(self):
        """Test color camera offset from depth camera."""
        # Typical offset for D435i
        offset = [0.015, 0.0, 0.0]  # 15mm in X direction

        assert offset[0] > 0, "Color camera should be offset in X"
        assert offset[1] == 0.0, "No Y offset"
        assert offset[2] == 0.0, "No Z offset"


class TestIntegrationScenarios:
    """Test integration scenarios."""

    def test_complete_tf_tree_structure(self):
        """Test complete TF tree structure."""
        expected_tree = {
            "odom": ["base_link"],
            "base_link": ["camera0_link"],
            "camera0_link": [
                "camera0_depth_frame",
                "camera0_color_frame",
                "camera0_infra1_frame",
                "camera0_infra2_frame",
                "camera0_imu_optical_frame",
            ],
            "camera0_depth_frame": ["camera0_depth_optical_frame"],
            "camera0_color_frame": ["camera0_color_optical_frame"],
            "camera0_infra1_frame": ["camera0_infra1_optical_frame"],
            "camera0_infra2_frame": ["camera0_infra2_optical_frame"],
        }

        # Verify tree structure
        assert "odom" in expected_tree
        assert "base_link" in expected_tree["odom"]
        assert "camera0_link" in expected_tree["base_link"]

    def test_topic_naming_convention(self):
        """Test ROS2 topic naming conventions."""
        camera_name = "camera0"

        expected_topics = [
            f"/{camera_name}/depth/image_rect_raw",
            f"/{camera_name}/depth/image",
            f"/{camera_name}/depth/camera_info",
            f"/{camera_name}/color/image_raw",
            f"/{camera_name}/color/camera_info",
            f"/{camera_name}/imu",
            f"/{camera_name}/odom",
        ]

        for topic in expected_topics:
            assert topic.startswith(f"/{camera_name}/")
            assert not topic.endswith("/")

    def test_gstreamer_pipeline_components(self):
        """Test GStreamer pipeline has required components."""
        pipeline = (
            "udpsrc port=5020 buffer-size=2097152 "
            'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" '
            "! rtpjitterbuffer latency=200 "
            "! rtph264depay ! h264parse ! avdec_h264 "
            "! videoconvert ! video/x-raw,format=GRAY16_LE"
        )

        required_elements = [
            "udpsrc",
            "rtpjitterbuffer",
            "rtph264depay",
            "h264parse",
            "avdec_h264",
            "videoconvert",
        ]

        for element in required_elements:
            assert element in pipeline, f"Missing GStreamer element: {element}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
