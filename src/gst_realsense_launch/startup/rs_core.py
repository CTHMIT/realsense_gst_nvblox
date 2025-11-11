#!/usr/bin/env python3
"""RealSense Streaming Core Module with Strategy Pattern

This module implements the Strategy Pattern for different encoding and streaming approaches.
Includes integrated depth receiver and Y8I stereo streaming functionality.
"""

import shlex
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Optional

from utils.logger import LOGGER

# ============================================================================
# GStreamer Initialization
# ============================================================================
GST_AVAILABLE: bool = False
GST_INITIALIZED: bool = False
Gst: Any = None
GLib: Any = None


def init_gstreamer() -> bool:
    """Initialize GStreamer after ROS2 is initialized."""
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


# ============================================================================
# ROS2 Imports (Optional)
# ============================================================================
ROS2_AVAILABLE: bool = False
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import Header

    ROS2_AVAILABLE = True
except ImportError:
    ROS2_AVAILABLE = False
    Node = object  # type: ignore


# Import after defining sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader


# ============================================================================
# RTP Payload Type Configuration
# ============================================================================
def get_pt(stream_type: str, encoding_name: str, config_loader=None) -> int:
    """Get RTP payload type from config or defaults."""
    if config_loader:
        payload_types: dict[str, int] = config_loader.get("streaming.rtp.payload_types", {})
        key = f"{stream_type.lower()}_{encoding_name.lower()}"
        if key in payload_types:
            return payload_types[key]

    # Fallback to hardcoded defaults
    RTP_PT = {
        ("depth", "H264"): 96,
        ("color", "H264"): 98,
        ("infra1", "H264"): 97,
        ("infra2", "H264"): 99,
        ("infra_stereo", "H264"): 100,
    }
    return RTP_PT.get((stream_type.lower(), encoding_name.upper()), 96)


# ============================================================================
# Encoder Strategies
# ============================================================================
class EncoderStrategy(ABC):
    """Abstract base class for video encoding strategies."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this encoder is available on the system."""
        pass

    @abstractmethod
    def get_pipeline_element(self, bitrate: int | None, pt: int, config: dict = None) -> str:
        """Get GStreamer pipeline element string for this encoder."""
        pass

    @abstractmethod
    def get_encoding_name(self) -> str:
        """Get RTP encoding name (e.g., 'H264')."""
        pass


class NvH264EncoderStrategy(EncoderStrategy):
    """NVIDIA hardware H.264 encoder strategy."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "nvh264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        config = config or {}
        tune = config.get("tune", "zerolatency")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"nvh264enc preset=low-latency-hq rc-mode=cbr bitrate={bitrate} "
            f"gop-size={key_int_max} bframes=0 "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H264"


class X264EncoderStrategy(EncoderStrategy):
    """Software H.264 encoder strategy (tested and working)."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "x264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        """Get software H.264 encoder pipeline with tested parameters."""
        config = config or {}
        tune = config.get("tune", "zerolatency")
        speed_preset = config.get("speed_preset", "ultrafast")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"x264enc tune={tune} speed-preset={speed_preset} bitrate={bitrate} "
            f"key-int-max={key_int_max} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H264"


class EncoderFactory:
    """Factory for creating encoder strategies."""

    @staticmethod
    def create_encoder(preference: str = "auto", stream_type: str = "color") -> EncoderStrategy:
        """Create encoder strategy based on preference and stream type."""

        if preference == "nvh264enc":
            nv_enc = NvH264EncoderStrategy()
            if nv_enc.is_available():
                return nv_enc
            raise RuntimeError("NVIDIA H.264 encoder (nvh264enc) not available")

        if preference == "x264enc":
            x264_enc = X264EncoderStrategy()
            if x264_enc.is_available():
                return x264_enc
            raise RuntimeError("Software H.264 encoder (x264enc) not available")

        # Auto selection
        if preference == "auto":
            nv_enc = NvH264EncoderStrategy()
            if nv_enc.is_available():
                return nv_enc

            x264_enc = X264EncoderStrategy()
            if x264_enc.is_available():
                return x264_enc

            raise RuntimeError(
                "No H.264 encoder available. Install gstreamer1.0-plugins-good "
                "or gstreamer1.0-plugins-bad"
            )

        raise ValueError(f"Unknown encoder preference: {preference}")


# ============================================================================
# Stream Pipeline Strategies
# ============================================================================
class StreamPipelineStrategy(ABC):
    """Abstract base class for stream pipeline building strategies."""

    def __init__(self, config_loader=None):
        """Initialize strategy with optional config loader."""
        self.config_loader = config_loader

    @abstractmethod
    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 4000,
    ) -> str:
        """Build GStreamer pipeline for sending video stream."""
        pass

    @abstractmethod
    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build GStreamer pipeline for receiving video stream."""
        pass


class DepthStreamStrategy(StreamPipelineStrategy):
    """Strategy for depth stream (using tested v4l2-ctl + H.264 pipeline)."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 8000,
    ) -> str:
        """Build depth stream sender pipeline using tested method."""

        # Get config parameters
        config = {}
        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }
        else:
            udp_config = {"sync": "false", "async": "false"}

        # Use tested v4l2-ctl method for depth (Z16 format)
        fourcc_clean = fourcc.strip().ljust(4)

        # v4l2-ctl command to capture raw depth data
        v4l2_cmd = (
            f"v4l2-ctl -d {shlex.quote(device)} "
            f"--set-fmt-video=width={width},height={height},pixelformat='{fourcc_clean}' "
            f"--stream-mmap --stream-count=0 --stream-to=-"
        )

        pt = get_pt("depth", "H264", self.config_loader)
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        gst_pipeline = (
            f"fdsrc ! "
            f"queue max-size-buffers=2 ! "
            f"videoparse width={width} height={height} format=gray16-le framerate={fps}/1 ! "
            f"{encoder_pipeline} ! "
            f"udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

        return f"{v4l2_cmd} | gst-launch-1.0 -e {gst_pipeline}"

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build depth stream receiver pipeline."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4

        pt = get_pt("depth", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY16_LE"
        )


class ColorStreamStrategy(StreamPipelineStrategy):
    """Strategy for color stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 4000,
    ) -> str:
        """Build color stream sender pipeline."""

        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        fourcc_cleaned = fourcc.strip().upper()
        pt = get_pt("color", "H264", self.config_loader)
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        format_map = {
            "YUYV": "YUY2",
            "YUY2": "YUY2",
            "UYVY": "UYVY",
            "GREY": "GRAY8",
            "Y8": "GRAY8",
        }
        gst_format = format_map.get(fourcc_cleaned, "YUY2")

        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format={gst_format},width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build color stream receiver pipeline."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
            n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4
            n_threads = 4

        pt = get_pt("color", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert n-threads={n_threads} ! video/x-raw,format=BGR"
        )


class IRStreamStrategy(StreamPipelineStrategy):
    """Strategy for infrared stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 2000,
    ) -> str:
        """Build IR stream sender pipeline."""

        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        fourcc_cleaned = fourcc.strip().upper()
        pt = get_pt("ir", "H264")
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        format_map = {
            "GREY": "GRAY8",
            "Y8": "GRAY8",
        }
        gst_format = format_map.get(fourcc_cleaned, "GRAY8")

        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format={gst_format},width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build IR stream receiver pipeline."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4

        pt = get_pt("ir", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class Y8IStreamStrategy(StreamPipelineStrategy):
    """Strategy for Y8I interleaved infrared stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 2000,
    ) -> str:
        """Build Y8I sender pipeline (sends interleaved stereo as-is).

        Y8I format has width = 2 * single_ir_width (e.g., 1280x480 for two 640x480 images)
        We send the full Y8I frame, and the receiver will split it.
        """
        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        pt = get_pt("infra_stereo", "H264")
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        # Y8I is GRAY8 format but with double width
        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format=GRAY8,width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build Y8I receiver pipeline (receives full interleaved frame)."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4

        pt = get_pt("infra_stereo", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class StreamStrategyFactory:
    """Factory for creating stream pipeline strategies."""

    @staticmethod
    def create_strategy(stream_type: str, config_loader=None) -> StreamPipelineStrategy:
        """Create appropriate stream strategy based on type."""
        stream_type_lower = stream_type.lower()

        if stream_type_lower == "depth":
            return DepthStreamStrategy(config_loader)
        elif stream_type_lower == "color":
            return ColorStreamStrategy(config_loader)
        elif stream_type_lower == "infra_stereo":
            return Y8IStreamStrategy(config_loader)
        elif stream_type_lower in ["ir", "infra"]:
            return IRStreamStrategy(config_loader)
        else:
            return ColorStreamStrategy(config_loader)


# ============================================================================
# Depth Receiver Node (ROS2 Integration)
# ============================================================================
class DepthReceiverNode(Node):
    """ROS2 node for depth streaming with GStreamer."""

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
        """Initialize the depth receiver node."""
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

        # Statistics
        self.frame_count: int = 0
        self.last_frame_time: float = time.time()
        self.last_log_time: float = time.time()
        self.error_count: int = 0
        self.last_error_time: float = 0.0

        # Queue management - keep only latest frame
        self.frame_queue: Queue[Image] = Queue(maxsize=1)
        self.running: bool = True

        # Watchdog for pipeline health
        self.last_buffer_time: float = time.time()
        self.watchdog_timeout: float = 10.0

        # Buffer pool for reusing message objects
        self.message_pool: list[Image] = []
        self.max_pool_size: int = 10

        # QoS configuration
        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.VOLATILE,
        )

        # Publishers
        self.image_pub = self.create_publisher(
            Image, f"/{camera_name}/depth/image_rect_raw", qos_profile
        )
        self.info_pub = self.create_publisher(
            CameraInfo, f"/{camera_name}/depth/camera_info", qos_profile
        )

        LOGGER.info("Initializing GStreamer pipeline...")

        self.pipeline: Any = None
        self.appsink: Any = None
        self.bus: Any = None

        self._build_pipeline()
        self._start_pipeline()

        # Consumer thread with proper exception handling
        self.consumer_thread: threading.Thread = threading.Thread(
            target=self._consume_frames_safe, daemon=True
        )
        self.consumer_thread.start()

        # Watchdog thread
        self.watchdog_thread: threading.Thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self.watchdog_thread.start()

        # Publishing timer
        self.timer = self.create_timer(0.001, self._publish_frames)

        # Memory cleanup timer (every 30 seconds)
        self.cleanup_timer = self.create_timer(30.0, self._periodic_cleanup)

        LOGGER.info(f"✓ Depth Receiver Started")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Resolution: {width}x{height}")
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
            LOGGER.info("⚠ Using software decoder")

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

        # Set up bus for monitoring
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message::error", self._on_bus_error)
        self.bus.connect("message::warning", self._on_bus_warning)

        self.appsink.connect("new-sample", self._on_new_sample_callback)

    def _on_bus_error(self, bus: Any, message: Any) -> None:
        """Handle pipeline errors."""
        err, debug = message.parse_error()
        LOGGER.error(f"GStreamer Error: {err.message}")
        if debug:
            LOGGER.debug(f"Debug info: {debug}")
        self.error_count += 1
        self.last_error_time = time.time()

    def _on_bus_warning(self, bus: Any, message: Any) -> None:
        """Handle pipeline warnings."""
        warn, debug = message.parse_warning()
        LOGGER.warning(f"GStreamer Warning: {warn.message}")
        if debug:
            LOGGER.debug(f"Debug info: {debug}")

    def _start_pipeline(self) -> None:
        """Start the GStreamer pipeline."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start pipeline")
        LOGGER.info("✓ Pipeline started")

    def _on_new_sample_callback(self, appsink: Any) -> Gst.FlowReturn:
        """Callback when new sample arrives from GStreamer."""
        try:
            sample = appsink.emit("pull-sample")
            if not sample:
                return Gst.FlowReturn.ERROR

            buffer = sample.get_buffer()
            if not buffer:
                return Gst.FlowReturn.ERROR

            self.last_buffer_time = time.time()

            # Get buffer data
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                return Gst.FlowReturn.ERROR

            try:
                # Create or reuse Image message
                if self.message_pool:
                    msg = self.message_pool.pop()
                else:
                    msg = Image()

                # Set header
                msg.header.stamp = self.get_clock().now().to_msg()
                msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"

                # Set image properties
                msg.height = self.height
                msg.width = self.width
                msg.encoding = "16UC1"
                msg.is_bigendian = 0
                msg.step = self.width * 2

                # Copy data
                msg.data = bytes(map_info.data)

                # Try to add to queue (drop if full)
                try:
                    self.frame_queue.put_nowait(msg)
                except:
                    self._return_message_to_pool(msg)

            finally:
                buffer.unmap(map_info)

            return Gst.FlowReturn.OK

        except Exception as e:
            LOGGER.error(f"Error in sample callback: {e}")
            return Gst.FlowReturn.ERROR

    def _return_message_to_pool(self, msg: Image) -> None:
        """Return message to pool for reuse."""
        if len(self.message_pool) < self.max_pool_size:
            self.message_pool.append(msg)

    def _consume_frames_safe(self) -> None:
        """Safe wrapper for frame consumption."""
        try:
            self._consume_frames()
        except Exception as e:
            LOGGER.error(f"Fatal error in consumer thread: {e}")
            import traceback

            traceback.print_exc()

    def _consume_frames(self) -> None:
        """Consume frames from queue (runs in separate thread)."""
        while self.running:
            try:
                # Just sleep and let the timer-based publisher handle everything
                time.sleep(0.1)
            except Exception as e:
                LOGGER.error(f"Error in consumer: {e}")

    def _watchdog_loop(self) -> None:
        """Monitor pipeline health."""
        while self.running:
            time.sleep(5.0)

            if time.time() - self.last_buffer_time > self.watchdog_timeout:
                LOGGER.warning("No frames received for 10 seconds - pipeline may be stalled")
                self.last_buffer_time = time.time()

    def _publish_frames(self) -> None:
        """Publish frames from queue (called by timer)."""
        try:
            msg = self.frame_queue.get_nowait()

            # Publish image
            self.image_pub.publish(msg)

            # Publish camera info every 10 frames
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
                LOGGER.info(
                    f"Depth: {self.frame_count} frames, {fps:.1f} FPS, errors: {self.error_count}"
                )
                self.last_log_time = current_time

            # Return message to pool
            self._return_message_to_pool(msg)

        except Empty:
            pass

    def _periodic_cleanup(self) -> None:
        """Periodic cleanup of resources."""
        LOGGER.debug(f"Pool: {len(self.message_pool)}/{self.max_pool_size}, Queue: {self.frame_queue.qsize()}")

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

    def stop(self) -> None:
        """Stop the receiver and cleanup resources."""
        LOGGER.info("Stopping depth receiver...")
        self.running = False

        if hasattr(self, "consumer_thread") and self.consumer_thread.is_alive():
            self.consumer_thread.join(timeout=2.0)

        if hasattr(self, "watchdog_thread") and self.watchdog_thread.is_alive():
            self.watchdog_thread.join(timeout=2.0)

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            time.sleep(0.5)
            self.pipeline = None

        if self.bus:
            self.bus.remove_signal_watch()
            self.bus = None

        while not self.frame_queue.empty():
            try:
                msg = self.frame_queue.get_nowait()
                self._return_message_to_pool(msg)
            except Empty:
                break

        self.message_pool.clear()
        LOGGER.info("✓ Depth receiver stopped")