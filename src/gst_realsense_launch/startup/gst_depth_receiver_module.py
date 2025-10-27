#!/usr/bin/env python3
"""GStreamer Python Depth Receiver Module

This module provides a direct GStreamer-based depth receiver that bypasses gscam
for 16-bit depth support. It integrates with the existing RealSense streaming architecture.

IMPORTANT: Only H.264 encoding is supported (JPEG2000 and H.265 removed)

Pipeline Flow:
  Sender: Z16 → GRAY16_LE → I420 → x264enc → RTP (pt=96) → UDP
  Receiver: UDP → RTP → H.264 decode → GRAY16_LE → ROS2 16UC1

Why this is needed:
- gscam only supports: rgb8, bgr8, rgba8, bgra8, mono8
- gscam does NOT support: 16UC1, mono16 (required for depth)
- This module uses GStreamer Python bindings to publish 16-bit depth directly
"""

import time
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import CameraInfo, Image
    from std_msgs.msg import Header

    ROS2_AVAILABLE = True
except ImportError:
    ROS2_AVAILABLE = False

from utils.gst_utils import (
    GstElement,
    GstFlowReturn,
    GstPipeline,
    load_gst,
    require_plugins,
)
from utils.logger import LOGGER

try:
    Gst, GLib, GST_AVAILABLE = load_gst()
except Exception as e:
    GST_AVAILABLE = False
    LOGGER.error(f"Failed to load GStreamer: {e}")


class GStreamerDepthReceiverNode(Node):
    """ROS2 node that receives 16-bit depth via GStreamer Python bindings.

    This node bypasses gscam limitations by directly pulling samples from
    GStreamer appsink and publishing them as ROS2 sensor_msgs/Image.

    Supported encoding: H.264 ONLY (JPEG2000 and H.265 removed)
    Pipeline: Z16 → GRAY16_LE → H.264 → GRAY16_LE → ROS2 16UC1

    Matches sender: Z16 → GRAY16_LE → I420 → x264enc → RTP (pt=96) → UDP
    """

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
        super().__init__(f"{camera_name}_depth_receiver")

        self.port = port
        self.width = width
        self.height = height
        self.camera_name = camera_name
        self.encoding = encoding.lower()
        self.config_loader = config_loader
        self.intrinsics = intrinsics

        # Validate encoding - only H.264 is supported
        if self.encoding != "h264":
            LOGGER.error(f"Unsupported encoding: {encoding}")
            LOGGER.error("This receiver only supports H.264 encoding for depth")
            LOGGER.error("Please use H.264 encoding in your sender")
            raise ValueError(f"Unsupported encoding: {encoding}. Only 'h264' is supported.")

        self.frame_count = 0
        self.last_frame_time = time.time()
        self.last_log_time = time.time()

        # ROS2 publishers
        self.image_pub = self.create_publisher(Image, f"/{camera_name}/depth/image_rect_raw", 10)
        self.info_pub = self.create_publisher(CameraInfo, f"/{camera_name}/depth/camera_info", 10)

        # Verify GStreamer is available
        if not GST_AVAILABLE or Gst is None:
            LOGGER.error("GStreamer Python bindings not available!")
            raise RuntimeError("Install: sudo apt install python3-gi python3-gst-1.0")

        # GStreamer is already initialized by load_gst() at module level
        # No need to call Gst.init(None) again
        self.pipeline: GstPipeline | None = None
        self.appsink: GstElement | None = None
        self.loop = None

        # Build and start pipeline
        self._build_pipeline()
        self._start_pipeline()

        LOGGER.info(f"✓ GStreamer Depth Receiver Started")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Encoding: H.264 only")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  Topic: /{camera_name}/depth/image_rect_raw")
        LOGGER.info(f"  Format: 16UC1 (16-bit depth)")
        LOGGER.info(f"  Pipeline: Z16 → GRAY16_LE → H.264 → GRAY16_LE → ROS2 16UC1")

    def _build_pipeline(self) -> None:
        """Build GStreamer pipeline for H.264 depth reception.

        Optimized for: Z16 → GRAY16_LE → H.264 → GRAY16_LE → ROS2 16UC1
        This matches the sender pipeline: Z16 → GRAY16_LE → I420 → x264enc → RTP
        """

        # Get configuration
        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 10000000),
            30000000,
        )
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.depth.drop_on_latency", False
        )
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        drop_str = "true" if drop_on_latency else "false"

        # Only H.264 is supported for depth
        # Build caps and decoder for H.264
        payload = payload_types.get("depth_h264", 96)
        caps_str = (
            f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}"
        )
        decoder_elements = (
            f"rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads}"
        )

        # Complete pipeline: UDP → RTP → H.264 decode → GRAY16_LE → appsink
        pipeline_str = (
            f"udpsrc port={self.port} buffer-size={buffer_size} "
            f'caps="{caps_str}" '
            f"! {decoder_elements} "
            f"! queue max-size-buffers=4 leaky=downstream "
            f"! videoconvert n-threads=4 "
            f"! video/x-raw,format=GRAY16_LE,width={self.width},height={self.height},framerate=30/1 "
            f"! appsink name=sink emit-signals=true drop=true max-buffers=1"
        )

        LOGGER.debug(f"Pipeline: {pipeline_str}")
        LOGGER.info(f"  H.264 depth pipeline: RTP → H.264 decode → GRAY16_LE → 16UC1")

        # Create pipeline
        self.pipeline = Gst.parse_launch(pipeline_str)
        if not self.pipeline:
            LOGGER.error("Failed to create GStreamer pipeline")
            raise RuntimeError("Failed to create pipeline")

        # Get appsink
        self.appsink = self.pipeline.get_by_name("sink")
        if not self.appsink:
            LOGGER.error("Failed to get appsink")
            raise RuntimeError("Failed to get appsink")

        # Connect to new-sample signal
        self.appsink.connect("new-sample", self._on_new_sample)

    def _start_pipeline(self) -> None:
        """Start the GStreamer pipeline."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            LOGGER.error("Failed to start pipeline")
            raise RuntimeError("Failed to start pipeline")

        LOGGER.info("✓ Pipeline started successfully")

    def _on_new_sample(self, appsink) -> GstFlowReturn:
        """Callback when new sample (frame) is available."""
        sample = appsink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR

        # Get buffer
        buf = sample.get_buffer()
        if not buf:
            return Gst.FlowReturn.ERROR

        # Get caps (format info)
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")

        # Map buffer to numpy array
        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            LOGGER.warning("Failed to map buffer")
            return Gst.FlowReturn.ERROR

        try:
            # Convert to numpy array (16-bit unsigned)
            # GRAY16_LE = 16-bit little-endian grayscale
            depth_data = np.frombuffer(map_info.data, dtype=np.uint16)
            depth_image = depth_data.reshape((height, width))

            # Create ROS2 Image message
            msg = Image()
            msg.header = Header()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = f"{self.camera_name}_depth_optical_frame"
            msg.height = height
            msg.width = width
            msg.encoding = "16UC1"  # 16-bit unsigned, single channel
            msg.is_bigendian = 0  # Little-endian
            msg.step = width * 2  # bytes per row (2 bytes per pixel)
            msg.data = depth_image.tobytes()

            # Publish
            self.image_pub.publish(msg)

            # Publish camera info
            info_msg = self._create_camera_info(msg.header)
            self.info_pub.publish(info_msg)

            # Stats (log every second)
            self.frame_count += 1
            now = time.time()
            if now - self.last_log_time > 1.0:
                fps = self.frame_count / (now - self.last_log_time)
                LOGGER.info(f"Depth @ {fps:.1f} fps")
                self.frame_count = 0
                self.last_log_time = now

        finally:
            buf.unmap(map_info)

        return Gst.FlowReturn.OK

    def _create_camera_info(self, header: Header) -> CameraInfo:
        """Create camera info message from intrinsics."""
        msg = CameraInfo()
        msg.header = header
        msg.height = self.height
        msg.width = self.width
        msg.distortion_model = "plumb_bob"

        if self.intrinsics:
            fx = self.intrinsics.fx
            fy = self.intrinsics.fy
            cx = self.intrinsics.ppx
            cy = self.intrinsics.ppy
            distortion = self.intrinsics.distortion
        else:
            # Default intrinsics
            fx = self.width * 0.6
            fy = self.height * 0.6
            cx = self.width * 0.5
            cy = self.height * 0.5
            distortion = [0.0, 0.0, 0.0, 0.0, 0.0]

        msg.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
        msg.d = distortion
        msg.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        msg.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]

        return msg

    def stop(self) -> None:
        """Stop the pipeline and cleanup."""
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            LOGGER.info("Pipeline stopped")


def check_gstreamer_python_available() -> bool:
    """Check if GStreamer Python bindings are available."""
    LOGGER.info(f"GStreamer available: {GST_AVAILABLE}")
    LOGGER.info(f"ROS2 available: {ROS2_AVAILABLE}")
    return GST_AVAILABLE and ROS2_AVAILABLE


def create_depth_receiver_node(
    port: int,
    width: int,
    height: int,
    camera_name: str,
    encoding: str,
    config_loader: Any,
    intrinsics: Any,
) -> GStreamerDepthReceiverNode | None:
    """Factory function to create depth receiver node."""

    try:
        require_plugins(
            "udpsrc",
            "rtpjitterbuffer",
            "rtph264depay",
            "h264parse",
            "avdec_h264",
            "videoconvert",
            "appsink",
        )
    except RuntimeError as e:
        LOGGER.error("GStreamer not available: %s", e)
        return None

    if not check_gstreamer_python_available():
        LOGGER.error("GStreamer Python bindings not available!")
        LOGGER.error("   Install with: sudo apt install python3-gi python3-gst-1.0")
        return None

    try:
        # Initialize ROS2 if not already done
        if not rclpy.ok():
            rclpy.init()

        node = GStreamerDepthReceiverNode(
            port=port,
            width=width,
            height=height,
            camera_name=camera_name,
            encoding=encoding,
            config_loader=config_loader,
            intrinsics=intrinsics,
        )
        return node

    except Exception as e:
        LOGGER.error(f"Failed to create depth receiver node: {e}")
        import traceback

        traceback.print_exc()
        return None
