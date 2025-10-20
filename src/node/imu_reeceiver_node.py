#!/usr/bin/env python3
"""IMU Data Receiver Node

Receives IMU data via UDP and publishes to ROS2 topic.
"""

import json
import socket
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Imu
from std_msgs.msg import Header


class IMUReceiverNode(Node):
    """ROS2 Node that receives IMU data via UDP and publishes to ROS2."""

    def __init__(self):
        super().__init__("imu_receiver")

        # Get parameters
        self.declare_parameter("imu_port", 5050)
        self.declare_parameter("local_ip", "0.0.0.0")
        self.declare_parameter("frame_id", "camera0_imu_optical_frame")

        self.imu_port = self.get_parameter("imu_port").value
        self.local_ip = self.get_parameter("local_ip").value
        self.frame_id = self.get_parameter("frame_id").value

        # QoS profile matching realsense2_camera driver
        self.qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            durability=DurabilityPolicy.VOLATILE,
        )

        # Create IMU publisher
        self.imu_pub = self.create_publisher(Imu, "imu", self.qos)

        # UDP socket for IMU data
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.local_ip, self.imu_port))
        self.socket.settimeout(1.0)

        # Start receiver thread
        self.running = True
        self.receiver_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receiver_thread.start()

        self.get_logger().info(f"IMU receiver listening on {self.local_ip}:{self.imu_port}")
        self.get_logger().info(f"Publishing to frame_id: {self.frame_id}")

        if self.local_ip == "0.0.0.0":
            self.get_logger().warn(
                "IMU receiver is listening on all interfaces. "
                "Set local_ip parameter to restrict access."
            )

    def _receive_loop(self):
        """Receive UDP packets with IMU data."""
        while self.running and rclpy.ok():
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
                if self.running:
                    self.get_logger().error(f"Error receiving data: {e}")

    def _handle_imu_data(self, data: dict):
        """Process and publish IMU data."""
        try:
            imu_msg = Imu()
            imu_msg.header = Header()
            imu_msg.header.stamp = self.get_clock().now().to_msg()
            imu_msg.header.frame_id = self.frame_id

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
            imu_msg.angular_velocity_covariance = [
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
            imu_msg.orientation_covariance[0] = -1.0  # No orientation

            self.imu_pub.publish(imu_msg)

        except Exception as e:
            self.get_logger().error(f"Error publishing IMU: {e}")

    def _handle_calibration_data(self, data: dict):
        """Log camera calibration data."""
        self.get_logger().info("Received camera calibration data", once=True)

    def shutdown(self):
        """Clean shutdown."""
        self.get_logger().info("Shutting down IMU receiver...")
        self.running = False

        if self.receiver_thread and self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=2)

        try:
            self.socket.close()
        except Exception:
            pass

        self.get_logger().info("IMU receiver shutdown complete")


def main(args=None):
    rclpy.init(args=args)

    node = IMUReceiverNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
