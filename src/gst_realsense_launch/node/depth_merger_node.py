#!/usr/bin/env python3
"""Depth Merger Node - Combines two 8-bit streams into 16-bit depth."""

import time

import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image


class DepthMergerNode(Node):
    def __init__(self):
        super().__init__("depth_merger_node")

        # 宣告參數
        self.declare_parameter("camera_name", "camera")
        self.declare_parameter("width", 640)
        self.declare_parameter("height", 480)

        # 獲取參數
        self.camera_name = self.get_parameter("camera_name").value
        self.width = self.get_parameter("width").value
        self.height = self.get_parameter("height").value

        self.bridge = CvBridge()
        self.high_byte_buffer = None
        self.low_byte_buffer = None
        self.high_byte_time = 0.0
        self.low_byte_time = 0.0
        self.camera_info = None

        # Subscribers
        self.sub_high = self.create_subscription(
            Image, f"/{self.camera_name}/depth_high/image_raw", self.high_byte_callback, 10
        )

        self.sub_low = self.create_subscription(
            Image, f"/{self.camera_name}/depth_low/image_raw", self.low_byte_callback, 10
        )

        self.sub_info = self.create_subscription(
            CameraInfo, f"/{self.camera_name}/depth_high/camera_info", self.camera_info_callback, 10
        )

        # Publishers
        self.pub_depth = self.create_publisher(
            Image, f"/{self.camera_name}/depth/image_rect_raw", 10
        )

        self.pub_info = self.create_publisher(
            CameraInfo, f"/{self.camera_name}/depth/camera_info", 10
        )

        # Timer to publish merged depth
        self.create_timer(0.033, self.merge_and_publish)  # ~30 Hz

        self.get_logger().info("Depth Merger Node started")
        self.get_logger().info(f"Camera name: {self.camera_name}")
        self.get_logger().info(f"Resolution: {self.width}x{self.height}")

    def high_byte_callback(self, msg):
        try:
            img = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono8")
            self.high_byte_buffer = img
            self.high_byte_time = time.time()
        except Exception as e:
            self.get_logger().error(f"High byte conversion error: {e}")

    def low_byte_callback(self, msg):
        try:
            img = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono8")
            self.low_byte_buffer = img
            self.low_byte_time = time.time()
        except Exception as e:
            self.get_logger().error(f"Low byte conversion error: {e}")

    def camera_info_callback(self, msg):
        # Update frame_id to depth
        msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"
        self.camera_info = msg

    def merge_and_publish(self):
        if self.high_byte_buffer is None or self.low_byte_buffer is None:
            return

        # Check time sync
        time_diff = abs(self.high_byte_time - self.low_byte_time)
        if time_diff > 0.1:
            self.get_logger().warn(f"Time mismatch: {time_diff*1000:.1f}ms")

        try:
            # Merge: depth = (high << 8) | low
            high_shifted = self.high_byte_buffer.astype(np.uint16) << 8
            depth_16bit = high_shifted | self.low_byte_buffer.astype(np.uint16)

            # Publish merged depth
            depth_msg = self.bridge.cv2_to_imgmsg(depth_16bit, encoding="mono16")
            depth_msg.header.stamp = self.get_clock().now().to_msg()
            depth_msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"

            self.pub_depth.publish(depth_msg)

            # Publish camera info
            if self.camera_info is not None:
                self.camera_info.header.stamp = depth_msg.header.stamp
                self.pub_info.publish(self.camera_info)

        except Exception as e:
            self.get_logger().error(f"Merge error: {e}")


def main(args=None):
    rclpy.init(args=args)
    node = DepthMergerNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
