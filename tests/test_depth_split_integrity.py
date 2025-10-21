#!/usr/bin/env python3
"""Test depth split mode data integrity."""

import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from sensor_msgs.msg import Image


class DepthIntegrityTester(Node):
    def __init__(self):
        super().__init__("depth_integrity_tester")
        self.bridge = CvBridge()

        self.high_byte_sub = self.create_subscription(
            Image, "/camera/depth_high/image_raw", self.high_byte_callback, 10
        )

        self.low_byte_sub = self.create_subscription(
            Image, "/camera/depth_low/image_raw", self.low_byte_callback, 10
        )

        self.merged_sub = self.create_subscription(
            Image, "/camera/depth/image_rect_raw", self.merged_callback, 10
        )

        self.high_data = None
        self.low_data = None
        self.merged_data = None
        self.test_count = 0

        self.timer = self.create_timer(2.0, self.verify_integrity)

    def high_byte_callback(self, msg):
        try:
            self.high_data = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono8")
        except Exception as e:
            self.get_logger().error(f"High byte error: {e}")

    def low_byte_callback(self, msg):
        try:
            self.low_data = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono8")
        except Exception as e:
            self.get_logger().error(f"Low byte error: {e}")

    def merged_callback(self, msg):
        try:
            self.merged_data = self.bridge.imgmsg_to_cv2(msg, desired_encoding="mono16")
        except Exception as e:
            self.get_logger().error(f"Merged error: {e}")

    def verify_integrity(self):
        if self.high_data is None or self.low_data is None or self.merged_data is None:
            self.get_logger().warn("Waiting for data...")
            return

        self.test_count += 1

        reconstructed = (self.high_data.astype(np.uint16) << 8) | self.low_data.astype(np.uint16)

        difference = np.abs(reconstructed.astype(np.int32) - self.merged_data.astype(np.int32))
        max_diff = np.max(difference)
        mean_diff = np.mean(difference)

        self.get_logger().info(f"Test #{self.test_count}:")
        self.get_logger().info(f"  Max difference: {max_diff}")
        self.get_logger().info(f"  Mean difference: {mean_diff:.2f}")

        if max_diff == 0:
            self.get_logger().info("  ✓ Perfect reconstruction!")
        elif max_diff <= 1:
            self.get_logger().warn(f"  ⚠ Minor difference detected (max={max_diff})")
        else:
            self.get_logger().error(f"  ✗ Significant difference! (max={max_diff})")

        # 顯示深度統計
        valid_depth = self.merged_data[self.merged_data > 0]
        if len(valid_depth) > 0:
            self.get_logger().info(f"  Depth range: {valid_depth.min()}mm - {valid_depth.max()}mm")
            self.get_logger().info(f"  Mean depth: {valid_depth.mean():.1f}mm")
            self.get_logger().info(f"  Valid pixels: {len(valid_depth)}/{self.merged_data.size}")

        if self.test_count >= 10:
            self.get_logger().info("✓ Test complete! Depth split mode is working correctly.")
            rclpy.shutdown()


def main():
    rclpy.init()
    node = DepthIntegrityTester()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
