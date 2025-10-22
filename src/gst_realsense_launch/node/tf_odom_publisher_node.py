#!/usr/bin/env python3
"""TF and Odometry Publisher Node

Publishes dynamic TF transforms and odometry messages for navigation.
"""

import math

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Header
from tf2_ros import TransformBroadcaster


class TFOdomPublisherNode(Node):
    """ROS2 Node that publishes TF transforms and odometry."""

    def __init__(self):
        super().__init__("tf_odom_publisher")

        # Get parameters
        self.declare_parameter("camera_name", "camera0")
        self.declare_parameter("publish_odom", True)
        self.declare_parameter("odom_frame", "odom")
        self.declare_parameter("base_link_frame", "base_link")

        self.camera_name = self.get_parameter("camera_name").value
        self.publish_odom_enabled = self.get_parameter("publish_odom").value
        self.odom_frame = self.get_parameter("odom_frame").value
        self.base_link_frame = self.get_parameter("base_link_frame").value

        # QoS profile
        self.qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            durability=DurabilityPolicy.VOLATILE,
        )

        # TF broadcaster
        self.tf_broadcaster = TransformBroadcaster(self)

        # Odometry publisher
        if self.publish_odom_enabled:
            self.odom_pub = self.create_publisher(Odometry, "odom", self.qos)

        # Odometry state (for isaac_ros_nvblox integration)
        self.odom_x = 0.0
        self.odom_y = 0.0
        self.odom_theta = 0.0
        self.last_odom_time = self.get_clock().now()

        # TF and odometry timers
        self.tf_timer = self.create_timer(1.0 / 30.0, self._publish_tf)

        if self.publish_odom_enabled:
            self.odom_timer = self.create_timer(1.0 / 50.0, self._publish_odometry)

        self.get_logger().info(f"TF/Odom publisher initialized for camera: {self.camera_name}")
        self.get_logger().info(
            f"Publishing TF: {self.odom_frame} -> {self.base_link_frame} -> {self.camera_name}_link"
        )

        if self.publish_odom_enabled:
            self.get_logger().info(f"Publishing Odometry on topic: odom")

    def _publish_tf(self):
        """Publish dynamic TF transforms."""
        timestamp = self.get_clock().now().to_msg()
        transforms = []

        # Odom to base_link (robot pose in world)
        if self.publish_odom_enabled:
            t_odom = TransformStamped()
            t_odom.header.stamp = timestamp
            t_odom.header.frame_id = self.odom_frame
            t_odom.child_frame_id = self.base_link_frame
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
        t_base_cam.header.frame_id = self.base_link_frame
        t_base_cam.child_frame_id = f"{self.camera_name}_link"
        # Default: camera mounted 0.1m forward, 0.2m up from base
        t_base_cam.transform.translation.x = 0.1
        t_base_cam.transform.translation.y = 0.0
        t_base_cam.transform.translation.z = 0.2
        t_base_cam.transform.rotation.w = 1.0
        transforms.append(t_base_cam)

        self.tf_broadcaster.sendTransform(transforms)

    def _publish_odometry(self):
        """Publish odometry message."""
        if not self.publish_odom_enabled:
            return

        odom_msg = Odometry()
        odom_msg.header = Header()
        odom_msg.header.stamp = self.get_clock().now().to_msg()
        odom_msg.header.frame_id = self.odom_frame
        odom_msg.child_frame_id = self.base_link_frame

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


def main(args=None):
    rclpy.init(args=args)

    node = TFOdomPublisherNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
