#!/usr/bin/env python3
"""Standalone GStreamer Depth Receiver for tmux

KEY FIXES:
1. Thread safety between GStreamer callbacks and ROS2 publishers
2. CRITICAL: Initialize GStreamer AFTER ROS2 to avoid threading conflicts
"""

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Any, TypeAlias

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import LOGGER

try:
    import rclpy
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import Header

    ROS2_AVAILABLE = True
except ImportError:
    ROS2_AVAILABLE = False

from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader

if TYPE_CHECKING:
    from gi.repository import Gst as GstType

    GstPipeline: TypeAlias = GstType.Pipeline
    GstElement: TypeAlias = GstType.Element
    GstFlowReturn: TypeAlias = GstType.FlowReturn
else:
    GstPipeline = Any
    GstElement = Any
    GstFlowReturn = int

GST_AVAILABLE: bool = False
GST_INITIALIZED: bool = False
Gst: Any | None = None
GLib: Any | None = None


def init_gstreamer():
    """Initialize GStreamer

    This is critical! GStreamer and ROS2 both set up threading and signal handling.
    If GStreamer initializes first, it conflicts with ROS2's setup.
    """
    global GST_AVAILABLE, GST_INITIALIZED, Gst, GLib

    if GST_INITIALIZED:
        return True

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib as _GLib
        from gi.repository import Gst as _Gst

        # Initialize GStreamer
        LOGGER.info("Initializing GStreamer...")
        _Gst.init(None)

        Gst = _Gst
        GLib = _GLib
        GST_INITIALIZED = True
        LOGGER.info("✓ GStreamer initialized successfully")
        return True

    except Exception as e:
        LOGGER.error("Failed to initialize GStreamer")
        LOGGER.error(f"Error details: {e}")
        return False


try:
    import gi

    gi.require_version("Gst", "1.0")
    GST_AVAILABLE = True
except:
    GST_AVAILABLE = False


class GStreamerDepthReceiverNode(Node):
    """ROS2 node that receives 16-bit depth via GStreamer Python bindings."""

    def __init__(
        self,
        port: int,
        width: int,
        height: int,
        camera_name: str,
        encoding: str,
        config_loader,
        intrinsics,
    ):
        # Initialize ROS2 Node first
        super().__init__(
            f"{camera_name}_depth_receiver",
            allow_undeclared_parameters=True,
            automatically_declare_parameters_from_overrides=True,
        )

        LOGGER.info("Creating GStreamerDepthReceiverNode...")

        # NOW we can safely initialize GStreamer (after ROS2 node is created)
        if not GST_INITIALIZED:
            if not init_gstreamer():
                raise RuntimeError("Failed to initialize GStreamer")

        if Gst is None:
            raise RuntimeError("GStreamer not available")

        self.port = port
        self.width = width
        self.height = height
        self.camera_name = camera_name
        self.encoding = encoding.lower()
        self.config_loader = config_loader
        self.intrinsics = intrinsics

        # Validate encoding
        if self.encoding != "h264":
            LOGGER.error(f"Unsupported encoding: {encoding}")
            raise ValueError(f"Unsupported encoding: {encoding}. Only 'h264' is supported.")

        LOGGER.info(f"Using encoding: {self.encoding} (H.264 only)")

        self.frame_count = 0
        self.last_frame_time = time.time()
        self.last_log_time = time.time()

        # Thread-safe queue for frames from GStreamer thread
        self.frame_queue: Queue = Queue(maxsize=2)
        self.publish_lock = threading.Lock()

        # ROS2 publishers with reentrant callback group
        callback_group = ReentrantCallbackGroup()
        self.image_pub = self.create_publisher(Image, f"/{camera_name}/depth/image_rect_raw", 10)
        self.info_pub = self.create_publisher(CameraInfo, f"/{camera_name}/depth/camera_info", 10)

        # Timer to process queued frames in ROS2 thread (200Hz for max throughput)
        self.timer = self.create_timer(
            0.005,  # 5ms = 200Hz (was 0.01/100Hz)
            self._process_frame_queue,
            callback_group=callback_group,
        )

        LOGGER.info("Initializing GStreamer depth receiver pipeline...")

        self.pipeline = None
        self.appsink = None
        self.running = True

        # Build and start pipeline
        self._build_pipeline()
        self._start_pipeline()

        LOGGER.info(f"✓ GStreamer Depth Receiver Started (Thread-Safe)")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Encoding: H.264 only")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  Topic: /{camera_name}/depth/image_rect_raw")

    def _build_pipeline(self) -> None:
        """Build GStreamer pipeline for H.264 depth reception."""
        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000),  # Increased default
            50000000,
        )
        max_threads = self.config_loader.get("streaming.processing.max_threads", 8)  # More threads
        latency = self.config_loader.get(
            "streaming.jitter_buffer.depth.latency", 50
        )  # Reduced from 200ms
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.depth.drop_on_latency", True  # Changed to True
        )
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        drop_str = "true" if drop_on_latency else "false"
        payload = payload_types.get("depth_h264", 96)

        caps_str = (
            f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}"
        )

        # Try hardware decoders first, fallback to software
        decoder_elements = None

        # Try VAAPI (Intel/AMD GPUs)
        if Gst.ElementFactory.find("vaapih264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! vaapih264dec"
            )
            LOGGER.info("Using VAAPI hardware H.264 decoder")
        # Try NVDEC (NVIDIA GPUs)
        elif Gst.ElementFactory.find("nvh264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! nvh264dec"
            )
            LOGGER.info("Using NVDEC hardware H.264 decoder")
        # Fallback to software
        else:
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads}"
            )
            LOGGER.info(
                "Using software H.264 decoder (install gstreamer-vaapi for HW acceleration)"
            )

        pipeline_str = (
            f"udpsrc port={self.port} buffer-size={buffer_size} "
            f'caps="{caps_str}" '
            f"! {decoder_elements} "
            f"! queue max-size-buffers=2 leaky=downstream "  # Reduced from 4
            f"! videoconvert n-threads={max_threads} "
            f"! video/x-raw,format=GRAY16_LE,width={self.width},height={self.height} "
            f"! appsink name=sink emit-signals=true drop=true max-buffers=1 sync=false"  # Added sync=false
        )

        LOGGER.debug(f"Pipeline: {pipeline_str}")
        LOGGER.info(f"  H.264 depth pipeline: RTP → H.264 decode → GRAY16_LE → 16UC1")

        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            raise RuntimeError("Failed to create pipeline")

        self.appsink = self.pipeline.get_by_name("sink")
        if not self.appsink:
            raise RuntimeError("Failed to get appsink")

        # Connect callback - THIS RUNS IN GSTREAMER THREAD!
        self.appsink.connect("new-sample", self._on_new_sample)

    def _start_pipeline(self) -> None:
        """Start the GStreamer pipeline."""
        if not self.pipeline:
            raise RuntimeError("Pipeline not created")

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start pipeline")

        LOGGER.info("✓ Pipeline started successfully")

    def _on_new_sample(self, appsink) -> GstFlowReturn:
        """GStreamer callback for new sample - RUNS IN GSTREAMER THREAD"""
        if not self.running:
            return Gst.FlowReturn.OK

        try:
            sample = appsink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.ERROR

            buffer = sample.get_buffer()
            if not buffer:
                return Gst.FlowReturn.ERROR

            # Extract buffer data
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                return Gst.FlowReturn.ERROR

            try:
                # Copy data from GStreamer buffer
                data = np.frombuffer(map_info.data, dtype=np.uint16).copy()

                # Queue frame data (non-blocking)
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except:
                        pass

                # Add new frame with timestamp
                self.frame_queue.put_nowait(
                    {"data": data, "timestamp": self.get_clock().now().to_msg()}
                )

            finally:
                buffer.unmap(map_info)

            return Gst.FlowReturn.OK

        except Exception as e:
            LOGGER.error(f"Error in _on_new_sample: {e}")
            return Gst.FlowReturn.ERROR

    def _process_frame_queue(self):
        """Process queued frames - RUNS IN ROS2 THREAD (via timer)

        Optimized for maximum throughput.
        """
        if not self.running:
            return

        try:
            # Get frame from queue (non-blocking)
            frame_data = self.frame_queue.get_nowait()

            # Reshape to 2D image
            depth_array = frame_data["data"].reshape((self.height, self.width))

            # Create ROS2 Image message
            msg = Image()
            msg.header = Header()
            msg.header.stamp = frame_data["timestamp"]
            msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"
            msg.height = self.height
            msg.width = self.width
            msg.encoding = "16UC1"
            msg.is_bigendian = False
            msg.step = self.width * 2
            msg.data = depth_array.tobytes()

            # Publish image (always)
            with self.publish_lock:
                self.image_pub.publish(msg)

                # Publish camera info less frequently (every 10th frame)
                if self.frame_count % 10 == 0:
                    info_msg = self._create_camera_info(msg.header)
                    self.info_pub.publish(info_msg)

            # Update stats
            self.frame_count += 1
            current_time = time.time()

            # Log every 60 frames (every 2 seconds at 30fps)
            if self.frame_count % 60 == 0:
                elapsed = current_time - self.last_log_time
                fps = 60.0 / elapsed if elapsed > 0 else 0
                LOGGER.info(f"Depth: {self.frame_count} frames, {fps:.1f} FPS")
                self.last_log_time = current_time

        except:
            # Queue empty - skip this cycle
            pass

    def _create_camera_info(self, header: Header) -> CameraInfo:
        """Create CameraInfo message from intrinsics."""
        info = CameraInfo()
        info.header = header
        info.width = self.intrinsics.width
        info.height = self.intrinsics.height

        # Camera matrix (K)
        info.k = [
            self.intrinsics.fx,
            0.0,
            self.intrinsics.ppx,
            0.0,
            self.intrinsics.fy,
            self.intrinsics.ppy,
            0.0,
            0.0,
            1.0,
        ]

        # Distortion coefficients
        info.d = self.intrinsics.distortion
        info.distortion_model = "plumb_bob"

        # Rectification matrix (identity)
        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

        # Projection matrix
        info.p = [
            self.intrinsics.fx,
            0.0,
            self.intrinsics.ppx,
            0.0,
            0.0,
            self.intrinsics.fy,
            self.intrinsics.ppy,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
        ]

        return info

    def stop(self):
        """Stop the receiver."""
        LOGGER.info("Stopping depth receiver...")
        self.running = False

        if self.appsink:
            try:
                self.appsink.disconnect_by_func(self._on_new_sample)
            except:
                pass

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

        LOGGER.info("✓ Depth receiver stopped")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Standalone GStreamer Depth Receiver for ROS2")
    parser.add_argument("--port", type=int, required=True, help="UDP port to receive on")
    parser.add_argument("--width", type=int, required=True, help="Image width")
    parser.add_argument("--height", type=int, required=True, help="Image height")
    parser.add_argument("--camera-name", required=True, help="Camera name for topics")
    parser.add_argument("--encoding", default="h264", help="Encoding: h264")
    parser.add_argument(
        "--config", default="src/config/config.yaml", help="Configuration file path"
    )
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


def check_availability():
    """Check if required dependencies are available."""
    gst_status = "✓" if GST_AVAILABLE else "✗"
    ros_status = "✓" if ROS2_AVAILABLE else "✗"

    LOGGER.info(f"GStreamer available: {GST_AVAILABLE} {gst_status}")
    LOGGER.info(f"ROS2 available: {ROS2_AVAILABLE} {ros_status}")

    return GST_AVAILABLE and ROS2_AVAILABLE


def main():
    """Main entry point."""

    # Check dependencies (but don't initialize GStreamer yet!)
    if not check_availability():
        LOGGER.error("Cannot start: Missing dependencies")
        sys.exit(1)

    LOGGER.info("✓ All dependencies available")

    args = parse_args()

    try:
        # Load configuration
        config_path = Path(args.config)
        if not config_path.is_file():
            LOGGER.error(f"Configuration file not found: {args.config}")
            sys.exit(1)

        config_loader = ConfigLoader(str(config_path))
        intrinsics = create_intrinsics(args, config_loader)

        LOGGER.info("=" * 50)
        LOGGER.info("GSTREAMER DEPTH RECEIVER (Thread-Safe v2)")
        LOGGER.info("=" * 50)
        LOGGER.info(f"Port: {args.port}")
        LOGGER.info(f"Resolution: {args.width}x{args.height}")
        LOGGER.info(f"Encoding: {args.encoding}")
        LOGGER.info(f"Camera: {args.camera_name}")
        LOGGER.info(f"Topic: /{args.camera_name}/depth/image_rect_raw")
        LOGGER.info(f"Format: 16UC1 (16-bit depth)")
        LOGGER.info("=" * 50)

        # Initialize ROS2 FIRST (before GStreamer!)
        LOGGER.info("Initializing ROS2...")
        rclpy.init()
        LOGGER.info("✓ ROS2 initialized")

        # Create node (GStreamer will be initialized inside the node constructor)
        LOGGER.info("Creating depth receiver node...")
        try:
            node = GStreamerDepthReceiverNode(
                port=args.port,
                width=args.width,
                height=args.height,
                camera_name=args.camera_name,
                encoding=args.encoding,
                config_loader=config_loader,
                intrinsics=intrinsics,
            )
        except Exception as e:
            LOGGER.error(f"Failed to create depth receiver node: {e}")
            import traceback

            traceback.print_exc()
            sys.exit(1)

        LOGGER.info("✓ Depth receiver node created successfully")
        LOGGER.info("Starting to receive and publish depth data...")
        LOGGER.info("Press Ctrl+C to stop")

        # Use MultiThreadedExecutor for thread safety
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(node)

        try:
            executor.spin()
        except KeyboardInterrupt:
            LOGGER.info("Received interrupt signal")

    except Exception as e:
        LOGGER.error(f"Fatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    finally:
        LOGGER.info("Shutting down depth receiver...")
        try:
            if "node" in locals():
                node.stop()
        except Exception as e:
            LOGGER.error(f"Error during node cleanup: {e}")

        try:
            if "executor" in locals():
                executor.shutdown()
        except Exception as e:
            LOGGER.error(f"Error during executor shutdown: {e}")

        try:
            if "node" in locals():
                node.destroy_node()
        except Exception as e:
            LOGGER.error(f"Error during node destroy: {e}")

        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception as e:
                LOGGER.error(f"Error during ROS2 shutdown: {e}")

        LOGGER.info("✓ Depth receiver stopped")


if __name__ == "__main__":
    main()
