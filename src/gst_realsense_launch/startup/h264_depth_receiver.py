#!/usr/bin/env python3
"""GStreamer Depth Receiver

1. QoS: BEST_EFFORT + KEEP_LAST=1 (avoid accumulation and retransmission delays)
2. Blocking pull: Single consumer thread (replaces timer polling)
3. Remove NumPy: Direct raw bytes (avoid copy overhead)
4. Queue maxsize=1: Always keep only the latest frame
5. Hardware decoder auto-detection: NVIDIA/VAAPI → CPU fallback
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional

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

GST_AVAILABLE: bool = False
GST_INITIALIZED: bool = False
Gst: Any = None
GLib: Any = None


def init_gstreamer() -> bool:
    """Initialize GStreamer after ROS2 is initialized.

    Returns:
        bool: True if initialization successful, False otherwise
    """
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
        LOGGER.info("✓ GStreamer initialized successfully")
        return True

    except Exception as e:
        LOGGER.error(f"Failed to initialize GStreamer: {e}")
        return False


try:
    import gi

    gi.require_version("Gst", "1.0")
    GST_AVAILABLE = True
except Exception:
    GST_AVAILABLE = False


class DepthReceiverNode(Node):
    """ROS2 node for depth streaming."""

    def __init__(
        self,
        port: int,
        width: int,
        height: int,
        camera_name: str,
        encoding: str,
        config_loader: ConfigLoader,
        intrinsics: CameraIntrinsics,
    ) -> None:
        """Initialize the depth receiver node.

        Args:
            port: UDP port to receive on
            width: Image width in pixels
            height: Image height in pixels
            camera_name: Camera name for topic naming
            encoding: Video encoding format (only 'h264' supported)
            config_loader: Configuration loader instance
            intrinsics: Camera intrinsics parameters
        """
        super().__init__(
            f"{camera_name}_depth_receiver",
            allow_undeclared_parameters=True,
            automatically_declare_parameters_from_overrides=True,
        )

        LOGGER.info("Creating DepthReceiverNode...")

        if not GST_INITIALIZED:
            if not init_gstreamer():
                raise RuntimeError("Failed to initialize GStreamer")

        if Gst is None:
            raise RuntimeError("GStreamer not available")

        self.port: int = port
        self.width: int = width
        self.height: int = height
        self.camera_name: str = camera_name
        self.encoding: str = encoding.lower()
        self.config_loader: ConfigLoader = config_loader
        self.intrinsics: CameraIntrinsics = intrinsics

        if self.encoding != "h264":
            raise ValueError(f"Unsupported encoding: {encoding}. Only 'h264' is supported.")

        self.frame_count: int = 0
        self.last_frame_time: float = time.time()
        self.last_log_time: float = time.time()

        self.frame_queue: Queue[Image] = Queue(maxsize=1)
        self.running: bool = True

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

        LOGGER.info("Initializing GStreamer pipeline...")

        self.pipeline: Any = None
        self.appsink: Any = None

        self._build_pipeline()
        self._start_pipeline()

        self.consumer_thread: threading.Thread = threading.Thread(
            target=self._consume_frames, daemon=True
        )
        self.consumer_thread.start()

        self.timer = self.create_timer(0.001, self._publish_frames)

        LOGGER.info(f"✓ Depth Receiver Started")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  QoS: BEST_EFFORT + KEEP_LAST=1")
        LOGGER.info(f"  Queue: maxsize=1 (latest frame only)")
        LOGGER.info(f"  Consumer: Blocking thread (no polling)")
        LOGGER.info(f"  Topic: /{camera_name}/depth/image_rect_raw")

    def _build_pipeline(self) -> None:
        """Build GStreamer pipeline with hardware decoder detection."""
        buffer_size: int = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000), 50000000
        )
        max_threads: int = self.config_loader.get("streaming.processing.max_threads", 8)
        latency: int = self.config_loader.get("streaming.jitter_buffer.depth.latency", 30)
        payload_types: dict[str, int] = self.config_loader.get("streaming.rtp.payload_types", {})
        payload: int = payload_types.get("depth_h264", 96)

        caps_str: str = (
            f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}"
        )

        decoder_elements: str | None = None

        # Try NVIDIA NVDEC first
        if Gst.ElementFactory.find("nvh264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! nvh264dec"
            )
            LOGGER.info("✓ Using NVIDIA NVDEC hardware decoder")
        # Try VAAPI (Intel/AMD)
        elif Gst.ElementFactory.find("vaapih264dec"):
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! vaapih264dec"
            )
            LOGGER.info("✓ Using VAAPI hardware decoder")
        # Fallback to software
        else:
            decoder_elements = (
                f"rtpjitterbuffer latency={latency} drop-on-latency=true "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads}"
            )
            LOGGER.info("⚠ Using software decoder (install gstreamer-vaapi for GPU acceleration)")

        pipeline_str: str = (
            f"udpsrc port={self.port} buffer-size={buffer_size} "
            f'caps="{caps_str}" '
            f"! {decoder_elements} "
            f"! queue max-size-buffers=1 leaky=downstream "
            f"! videoconvert n-threads={max_threads} "
            f"! video/x-raw,format=GRAY16_LE,width={self.width},height={self.height} "
            f"! appsink name=sink emit-signals=true drop=true max-buffers=1 sync=false"
        )

        LOGGER.debug(f"Pipeline: {pipeline_str}")

        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            raise RuntimeError("Failed to create pipeline")

        self.appsink = self.pipeline.get_by_name("sink")
        if not self.appsink:
            raise RuntimeError("Failed to get appsink")

        self.appsink.connect("new-sample", self._on_new_sample_callback)

    def _start_pipeline(self) -> None:
        """Start the GStreamer pipeline."""
        if not self.pipeline:
            raise RuntimeError("Pipeline not created")

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to set pipeline to PLAYING state")

        LOGGER.info("✓ Pipeline started (PLAYING)")

    def _on_new_sample_callback(self, sink: Any) -> Any:
        """GStreamer callback for new samples (signals the consumer thread).

        This callback runs in GStreamer's thread. It just returns OK to signal
        that a sample is ready. The actual processing happens in _consume_frames.

        Args:
            sink: The appsink element

        Returns:
            Gst.FlowReturn.OK
        """
        return Gst.FlowReturn.OK

    def _consume_frames(self) -> None:
        """
        Single consumer thread with blocking pull.

        Uses appsink.pull_sample() to block and wait for new frames
        """
        LOGGER.info("Consumer thread started (blocking mode)")

        while self.running:
            try:
                sample = self.appsink.emit("pull-sample")

                if sample is None:
                    continue

                buffer = sample.get_buffer()
                if not buffer:
                    continue

                success, map_info = buffer.map(Gst.MapFlags.READ)
                if not success:
                    continue

                try:
                    data_bytes: bytes = bytes(map_info.data)

                    # Create ROS2 Image message
                    msg = Image()
                    msg.header.stamp = self.get_clock().now().to_msg()
                    msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"
                    msg.height = self.height
                    msg.width = self.width
                    msg.encoding = "16UC1"
                    msg.is_bigendian = 0
                    msg.step = self.width * 2
                    msg.data = data_bytes

                    try:
                        self.frame_queue.put_nowait(msg)
                    except Exception as e:
                        LOGGER.warning(f"Frame queue full, dropping frame: {e}")

                finally:
                    buffer.unmap(map_info)

            except Exception as e:
                if self.running:
                    LOGGER.error(f"Error in consumer thread: {e}")
                if not self.running:
                    break

        LOGGER.info("Consumer thread stopped")

    def _publish_frames(self) -> None:
        """
        Publish frames from the queue.

        """
        try:
            # Non-blocking get
            msg: Image = self.frame_queue.get_nowait()

            # Publish image
            self.image_pub.publish(msg)

            # Publish camera info (every 10 frames)
            if self.frame_count % 10 == 0:
                info_msg: CameraInfo = self._create_camera_info(msg.header)
                self.info_pub.publish(info_msg)

            # Update stats
            self.frame_count += 1
            current_time: float = time.time()

            # Log every 60 frames
            if self.frame_count % 60 == 0:
                elapsed: float = current_time - self.last_log_time
                fps: float = 60.0 / elapsed if elapsed > 0 else 0
                LOGGER.info(f"Depth: {self.frame_count} frames, {fps:.1f} FPS")
                self.last_log_time = current_time

        except Empty as e:
            pass  # Queue empty, skip

    def _create_camera_info(self, header: Header) -> CameraInfo:
        """Create CameraInfo message from intrinsics.

        Args:
            header: ROS2 message header with timestamp and frame_id

        Returns:
            CameraInfo message populated with intrinsics
        """
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

    def stop(self) -> None:
        """Stop the receiver and cleanup resources."""
        LOGGER.info("Stopping depth receiver...")
        self.running = False

        # Wait for consumer thread
        if hasattr(self, "consumer_thread") and self.consumer_thread.is_alive():
            self.consumer_thread.join(timeout=2.0)

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

        LOGGER.info("✓ depth receiver stopped")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed command line arguments
    """
    parser = argparse.ArgumentParser(description="GStreamer Depth Receiver")
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


def create_intrinsics(args: argparse.Namespace, config_loader: ConfigLoader) -> CameraIntrinsics:
    """Create camera intrinsics from args or defaults.

    Args:
        args: Parsed command line arguments
        config_loader: Configuration loader instance

    Returns:
        Camera intrinsics parameters
    """
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


def check_availability() -> bool:
    """Check if required dependencies are available.

    Returns:
        True if all dependencies are available, False otherwise
    """
    gst_status = "✓" if GST_AVAILABLE else "✗"
    ros_status = "✓" if ROS2_AVAILABLE else "✗"

    LOGGER.info(f"GStreamer available: {GST_AVAILABLE} {gst_status}")
    LOGGER.info(f"ROS2 available: {ROS2_AVAILABLE} {ros_status}")

    return GST_AVAILABLE and ROS2_AVAILABLE


def main() -> None:
    """Main entry point."""
    if not check_availability():
        LOGGER.error("Cannot start: Missing dependencies")
        sys.exit(1)

    LOGGER.info("✓ All dependencies available")

    args = parse_args()

    node: DepthReceiverNode | None = None

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
        rclpy.init()
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

        LOGGER.info("✓ Node created successfully")
        LOGGER.info("Starting to receive and publish depth data...")
        LOGGER.info("Press Ctrl+C to stop")

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
        LOGGER.info("Shutting down depth receiver...")
        try:
            if node is not None:
                node.stop()
                node.destroy_node()
        except Exception as e:
            LOGGER.error(f"Error during cleanup: {e}")

        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception as e:
                LOGGER.error(f"Error during ROS2 shutdown: {e}")

        LOGGER.info("✓ Depth receiver stopped")


if __name__ == "__main__":
    main()
