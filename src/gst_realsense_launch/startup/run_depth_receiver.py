#!/usr/bin/env python3
"""GStreamer Depth Receiver"""

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Optional, TypeAlias, cast

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import LOGGER

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import Header

    ROS2_AVAILABLE = True
except ImportError:
    ROS2_AVAILABLE = False

from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader

if TYPE_CHECKING:
    from gi.repository import Gst as GstType
    from gi.repository import GstApp as GstAppType

    GstPipeline: TypeAlias = GstType.Pipeline
    GstElement: TypeAlias = GstType.Element
    GstFlowReturn: TypeAlias = GstType.FlowReturn
    GstAppSink: TypeAlias = GstAppType.AppSink
else:
    GstPipeline = Any
    GstElement = Any
    GstAppSink = Any
    GstFlowReturn = int


GST_AVAILABLE: bool = False
GST_INITIALIZED: bool = False
Gst: Any | None = None
GLib: Any | None = None


def init_gstreamer():
    """Initialize GStreamer after ROS2 is initialized"""
    global GST_AVAILABLE, GST_INITIALIZED, Gst, GLib

    if GST_INITIALIZED:
        return True

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib as _GLib
        from gi.repository import Gst as _Gst

        LOGGER.info("Initializing GStreamer...")
        _Gst.init(None)

        Gst = _Gst
        GLib = _GLib
        GST_INITIALIZED = True
        LOGGER.info("GStreamer initialized successfully")
        return True

    except Exception as e:
        LOGGER.error(f"Failed to initialize GStreamer: {e}")
        return False


try:
    import gi

    gi.require_version("Gst", "1.0")
    GST_AVAILABLE = True
except:
    GST_AVAILABLE = False


class DepthReceiverNode(Node):
    """ROS2 node for maximum FPS depth streaming."""

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
        super().__init__(
            f"{camera_name}_depth_receiver_optimized",
            allow_undeclared_parameters=True,
            automatically_declare_parameters_from_overrides=True,
        )

        LOGGER.info("Creating DepthReceiverNode...")

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

        if self.encoding != "h264":
            raise ValueError(f"Unsupported encoding: {encoding}. Only 'h264' is supported.")

        # Performance tracking
        self.frame_count = 0
        self.last_frame_time = time.time()
        self.last_log_time = time.time()

        self.frame_queue: Queue = Queue(maxsize=1)
        self.running = True

        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.VOLATILE,
        )

        self.image_pub = self.create_publisher(
            Image, f"/{camera_name}/depth/image_rect_raw", qos_profile
        )
        self.info_pub = self.create_publisher(
            CameraInfo, f"/{camera_name}/depth/camera_info", qos_profile
        )

        LOGGER.info("Initializing optimized GStreamer pipeline...")

        self.pipeline: GstPipeline | None = None
        self.appsink: GstAppSink | None = None

        # Build and start pipeline
        self._build_pipeline()
        self._start_pipeline()

        self.consumer_thread = threading.Thread(target=self._consume_frames, daemon=True)
        self.consumer_thread.start()

        LOGGER.info(f"Depth Receiver Started")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  QoS: BEST_EFFORT + KEEP_LAST=1")
        LOGGER.info(f"  Queue: maxsize=1 (latest frame only)")
        LOGGER.info(f"  Consumer: Blocking thread (no polling)")
        LOGGER.info(f"  Topic: /{camera_name}/depth/image_rect_raw")

    def _build_pipeline(self) -> None:
        """Build optimized GStreamer pipeline with hardware decoder detection."""
        buffer_size = min(self.config_loader.get("streaming.udp.buffer_size", 30000000), 50000000)
        max_threads = self.config_loader.get("streaming.processing.max_threads", 8)
        latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 30)
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        payload = payload_types.get("depth_h264", 96)

        caps_str = (
            f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}"
        )
        decoder_elements = None

        # Try NVIDIA NVDEC first
        if Gst.ElementFactory.find("nvh264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! nvh264dec"
            )
            LOGGER.info("Using NVIDIA NVDEC hardware decoder")
        # Try VAAPI (Intel/AMD)
        elif Gst.ElementFactory.find("vaapih264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! vaapih264dec"
            )
            LOGGER.info("Using VAAPI hardware decoder")
        # Fallback to software
        else:
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads}"
            )
            LOGGER.warning("Using software decoder (install gstreamer-vaapi for GPU acceleration)")

        pipeline_str = (
            f"udpsrc port={self.port} buffer-size={buffer_size} "
            f'caps="{caps_str}" '
            f"! {decoder_elements} "
            f"! queue max-size-buffers=1 leaky=downstream "
            f"! videoconvert n-threads={max_threads} "
            f"! video/x-raw,format=GRAY16_LE,width={self.width},height={self.height} "
            f"! appsink name=sink emit-signals=false drop=true max-buffers=1 sync=false"
        )

        LOGGER.debug(f"Pipeline: {pipeline_str}")

        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            raise RuntimeError("Failed to create pipeline")

        sink_el = self.pipeline.get_by_name("sink")
        if sink_el is None:
            raise RuntimeError("Failed to get appsink by name 'sink'")

        self.appsink = cast(GstAppSink, sink_el)

    def _start_pipeline(self) -> None:
        """Start the GStreamer pipeline."""
        if not self.pipeline:
            raise RuntimeError("Pipeline not created")

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set pipeline to PLAYING state")

        LOGGER.info("Pipeline started (PLAYING)")

    def _consume_frames(self) -> None:
        """
        use a blocking thread to consume frames from appsink

        """
        LOGGER.info("Consumer thread started (blocking mode)")

        while self.running:
            try:
                if self.appsink is None:
                    time.sleep(0.01)
                    continue

                sample = self.appsink.try_pull_sample(Gst.SECOND // 10)

                buffer = sample.get_buffer()
                if not buffer:
                    continue

                success, map_info = buffer.map(Gst.MapFlags.READ)
                if not success:
                    continue

                try:
                    data_bytes = bytes(map_info.data)

                    msg = Image()
                    msg.header.stamp = self.get_clock().now().to_msg()
                    msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"
                    msg.height = self.height
                    msg.width = self.width
                    msg.encoding = "16UC1"  # 16-bit depth
                    msg.is_bigendian = 0
                    msg.step = self.width * 2  # 2 bytes per pixel
                    msg.data = data_bytes

                    try:
                        self.frame_queue.put_nowait(msg)
                    except:
                        pass  # Queue full, drop

                finally:
                    buffer.unmap(map_info)

            except Exception as e:
                if self.running:
                    LOGGER.error(f"Error in consumer thread: {e}")

        LOGGER.info("Consumer thread stopped")

    def _publish_frames(self) -> None:
        """
        publish frames from the queue to ROS2 topics
        called by ROS2 timer
        """
        try:
            # Non-blocking get
            msg = self.frame_queue.get_nowait()

            # Publish image
            self.image_pub.publish(msg)

            # Publish camera info (each 10 frames)
            if self.frame_count % 10 == 0:
                info_msg = self._create_camera_info(msg.header)
                self.info_pub.publish(info_msg)

            # Update stats
            self.frame_count += 1
            current_time = time.time()

            # Log every 60 frames
            if self.frame_count % 60 == 0:
                elapsed = current_time - self.last_log_time
                fps = 60.0 / elapsed if elapsed > 0 else 0
                LOGGER.info(f"Depth: {self.frame_count} frames, {fps:.1f} FPS")
                self.last_log_time = current_time

        except Empty:
            pass  # Queue empty, skip

    def _create_camera_info(self, header: Header) -> CameraInfo:
        """Create CameraInfo message from intrinsics."""
        info = CameraInfo()
        info.header = header
        info.width = self.intrinsics.width
        info.height = self.intrinsics.height

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

        info.d = self.intrinsics.distortion
        info.distortion_model = "plumb_bob"

        info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

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
        LOGGER.info("Stopping optimized depth receiver...")
        self.running = False

        # Wait for consumer thread
        if hasattr(self, "consumer_thread") and self.consumer_thread.is_alive():
            self.consumer_thread.join(timeout=2.0)

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

        LOGGER.info("Optimized depth receiver stopped")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Optimized GStreamer Depth Receiver for Maximum FPS"
    )
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
        "--distortion", nargs=5, type=float, help="Distortion coefficients (5 values)"
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
    if not check_availability():
        LOGGER.error("Cannot start: Missing dependencies")
        sys.exit(1)

    LOGGER.info("All dependencies available")

    args = parse_args()

    try:
        # Load configuration
        config_path = Path(args.config)
        if not config_path.is_file():
            LOGGER.error(f"Configuration file not found: {args.config}")
            sys.exit(1)

        config_loader = ConfigLoader(str(config_path))
        intrinsics = create_intrinsics(args, config_loader)

        LOGGER.info("=" * 60)
        LOGGER.info("GSTREAMER DEPTH RECEIVER")
        LOGGER.info("=" * 60)
        LOGGER.info(f"Port: {args.port}")
        LOGGER.info(f"Resolution: {args.width}x{args.height}")
        LOGGER.info(f"Encoding: {args.encoding}")
        LOGGER.info(f"Camera: {args.camera_name}")
        LOGGER.info(f"Topic: /{args.camera_name}/depth/image_rect_raw")
        LOGGER.info(f"Format: 16UC1 (16-bit depth)")
        LOGGER.info("=" * 60)

        # Initialize ROS2 FIRST
        LOGGER.info("Initializing ROS2...")
        rclpy.init()
        LOGGER.info("ROS2 initialized")

        # Create node
        LOGGER.info("Creating optimized depth receiver node...")
        try:
            node = DepthReceiverNode(
                port=args.port,
                width=args.width,
                height=args.height,
                camera_name=args.camera_name,
                encoding=args.encoding,
                config_loader=config_loader,
                intrinsics=intrinsics,
            )
        except Exception as e:
            LOGGER.error(f"Failed to create node: {e}")
            import traceback

            traceback.print_exc()
            sys.exit(1)

        LOGGER.info("Node created successfully")
        LOGGER.info("Starting to receive and publish depth data...")
        LOGGER.info("Press Ctrl+C to stop")

        timer = node.create_timer(0.001, node._publish_frames)

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
        LOGGER.info("Shutting down optimized depth receiver...")
        try:
            if "node" in locals():
                node.stop()
                node.destroy_node()
        except Exception as e:
            LOGGER.error(f"Error during cleanup: {e}")

        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception as e:
                LOGGER.error(f"Error during ROS2 shutdown: {e}")

        LOGGER.info(" Optimized depth receiver stopped")


if __name__ == "__main__":
    main()
