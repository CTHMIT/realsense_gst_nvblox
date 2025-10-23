#!/usr/bin/env python3
"""Standalone GStreamer Depth Receiver for tmux

This script is designed to run in a tmux window to receive and publish
16-bit depth data via ROS2, bypassing gscam limitations.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader

try:
    from utils.logger import LOGGER
except ImportError:
    import logging

    LOGGER = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

try:
    import rclpy
    from gst_depth_receiver_module import create_depth_receiver_node

    DEPENDENCIES_AVAILABLE = True
except ImportError as e:
    LOGGER.error(f"Failed to import dependencies: {e}")
    DEPENDENCIES_AVAILABLE = False


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Standalone GStreamer Depth Receiver for ROS2")
    parser.add_argument("--port", type=int, required=True, help="UDP port to receive on")
    parser.add_argument("--width", type=int, required=True, help="Image width")
    parser.add_argument("--height", type=int, required=True, help="Image height")
    parser.add_argument("--camera-name", required=True, help="Camera name for topics")
    parser.add_argument("--encoding", default="h264", help="Encoding: h264, h265, or jpeg2000")
    parser.add_argument(
        "--config", default="src/config/config.yaml", help="Configuration file path"
    )

    # Camera intrinsics (optional)
    parser.add_argument("--fx", type=float, help="Focal length X")
    parser.add_argument("--fy", type=float, help="Focal length Y")
    parser.add_argument("--ppx", type=float, help="Principal point X")
    parser.add_argument("--ppy", type=float, help="Principal point Y")
    parser.add_argument(
        "--distortion",
        nargs=5,
        type=float,
        help="Distortion coefficients (5 values)",
    )

    return parser.parse_args()


def create_intrinsics(args, config_loader):
    """Create camera intrinsics from args or defaults."""
    if all([args.fx, args.fy, args.ppx, args.ppy]):
        distortion = args.distortion if args.distortion else [0.0, 0.0, 0.0, 0.0, 0.0]
        return CameraIntrinsics(
            width=args.width,
            height=args.height,
            fx=args.fx,
            fy=args.fy,
            ppx=args.ppx,
            ppy=args.ppy,
            distortion=distortion,
        )
    else:
        return config_loader.create_default_intrinsics(args.width, args.height)


def main():
    """Main entry point."""
    if not DEPENDENCIES_AVAILABLE:
        LOGGER.error("Cannot start: Missing dependencies")
        LOGGER.error("Install: sudo apt install python3-gi python3-gst-1.0")
        sys.exit(1)

    args = parse_args()

    try:
        # Load configuration
        config_loader = ConfigLoader(args.config)

        # Create intrinsics
        intrinsics = create_intrinsics(args, config_loader)

        LOGGER.info("=" * 50)
        LOGGER.info("GSTREAMER DEPTH RECEIVER (Standalone)")
        LOGGER.info("=" * 50)
        LOGGER.info(f"Port: {args.port}")
        LOGGER.info(f"Resolution: {args.width}x{args.height}")
        LOGGER.info(f"Encoding: {args.encoding}")
        LOGGER.info(f"Camera: {args.camera_name}")
        LOGGER.info(f"Topic: /{args.camera_name}/depth/image_rect_raw")
        LOGGER.info(f"Format: 16UC1 (16-bit depth)")
        LOGGER.info("=" * 50)

        # Initialize ROS2
        rclpy.init()

        # Create depth receiver node
        node = create_depth_receiver_node(
            port=args.port,
            width=args.width,
            height=args.height,
            camera_name=args.camera_name,
            encoding=args.encoding,
            config_loader=config_loader,
            intrinsics=intrinsics,
        )

        if not node:
            LOGGER.error("Failed to create depth receiver node")
            sys.exit(1)

        LOGGER.info("Depth receiver node created successfully")
        LOGGER.info("Starting to receive and publish depth data...")
        LOGGER.info("Press Ctrl+C to stop")

        # Spin the node (blocking)
        try:
            rclpy.spin(node)
        except KeyboardInterrupt:
            LOGGER.info("Received interrupt signal")

    except Exception as e:
        LOGGER.error(f"Fatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    finally:
        # Cleanup
        LOGGER.info("Shutting down depth receiver...")
        if "node" in locals() and node:
            try:
                node.stop()
                node.destroy_node()
            except Exception as e:
                LOGGER.error(f"Error during node cleanup: {e}")

        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception as e:
                LOGGER.error(f"Error during ROS2 shutdown: {e}")

        LOGGER.info("Depth receiver stopped")


if __name__ == "__main__":
    main()
