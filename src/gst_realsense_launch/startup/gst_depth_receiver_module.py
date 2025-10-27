#!/usr/bin/env python3
"""GStreamer Python Depth Receiver Module - H.264 Only

This module provides a direct GStreamer-based depth receiver that bypasses gscam
for 16-bit depth support. Simplified to support ONLY H.264 encoding.

Pipeline Flow:
  UDP → RTP (payload=96) → H.264 decode → GRAY16_LE → ROS2 16UC1

Why this is needed:
- gscam only supports: rgb8, bgr8, rgba8, bgra8, mono8
- gscam does NOT support: 16UC1, mono16 (required for depth)
- This module uses GStreamer Python bindings to publish 16-bit depth directly

Sender Pipeline (for reference):
  Z16 → GRAY16_LE → I420 → x264enc → rtph264pay → UDP
"""

import time
from typing import Any, Optional

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
    """ROS2 node that receives 16-bit depth via GStreamer Python bindings (H.264 only).

    This node bypasses gscam limitations by directly pulling samples from
    GStreamer appsink and publishing them as ROS2 sensor_msgs/Image.

    Supported encoding: H.264 only
    Output format: 16UC1 (16-bit unsigned, single channel)
    """

    def __init__(
        self,
        port: int,
        width: int,
        height: int,
        camera_name: str,
        config_loader,
        intrinsics,
    ):
        super().__init__(f"{camera_name}_depth_receiver")

        self.port = port
        self.width = width
        self.height = height
        self.camera_name = camera_name
        self.config_loader = config_loader
        self.intrinsics = intrinsics

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
        self.pipeline: GstPipeline | None = None
        self.appsink: GstElement | None = None
        self.loop = None

        # Build and start pipeline
        self._build_h264_pipeline()
        self._start_pipeline()

        LOGGER.info(f"✓ GStreamer Depth Receiver Started (H.264)")
        LOGGER.info(f"  Port: {port}")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  Topic: /{camera_name}/depth/image_rect_raw")
        LOGGER.info(f"  Format: 16UC1 (16-bit depth)")

    def _build_h264_pipeline(self) -> None:
        """Build GStreamer pipeline for H.264 depth reception.

        Pipeline breakdown:
        1. udpsrc: Receive UDP packets
        2. caps filter: Specify RTP H.264 format (payload=96)
        3. rtpjitterbuffer: Handle network jitter
        4. rtph264depay: Extract H.264 from RTP
        5. h264parse: Parse H.264 stream
        6. avdec_h264: Decode H.264 to raw video
        7. queue: Buffer management
        8. videoconvert: Format conversion
        9. caps filter: Ensure GRAY16_LE output
        10. appsink: Extract samples for ROS2 publishing
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
        payload = payload_types.get("depth_h264", 96)

        drop_str = "true" if drop_on_latency else "false"

        # Build caps string for RTP H.264
        caps_str = (
            f"application/x-rtp,"
            f"media=video,"
            f"clock-rate=90000,"
            f"encoding-name=H264,"
            f"payload={payload}"
        )

        # Build complete pipeline for H.264
        pipeline_str = (
            f"udpsrc port={self.port} buffer-size={buffer_size} "
            f'caps="{caps_str}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay "
            f"! h264parse "
            f"! avdec_h264 max-threads={max_threads} "
            f"! queue max-size-buffers=4 leaky=downstream "
            f"! videoconvert n-threads=4 "
            f"! video/x-raw,format=GRAY16_LE,width={self.width},height={self.height},framerate=30/1 "
            f"! appsink name=sink emit-signals=true drop=true max-buffers=1"
        )

        LOGGER.debug(f"Pipeline: {pipeline_str}")

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
        """Callback when new sample (frame) is available.

        This method:
        1. Extracts raw buffer from GStreamer
        2. Converts to numpy array (GRAY16_LE)
        3. Creates ROS2 Image message (16UC1)
        4. Publishes image and camera info
        """
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

            # Publish image
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
    """Factory function to create depth receiver node.

    Args:
        port: UDP port to receive on
        width: Image width
        height: Image height
        camera_name: Camera name for ROS2 topics
        encoding: Codec type (must be 'h264')
        config_loader: Configuration loader
        intrinsics: Camera intrinsics

    Returns:
        GStreamerDepthReceiverNode instance or None if failed
    """

    # Validate encoding
    if encoding.lower() != "h264":
        LOGGER.error(f"Unsupported encoding: {encoding}")
        LOGGER.error("This module only supports H.264 encoding")
        return None

    # Check required plugins
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
        LOGGER.error("GStreamer plugins not available: %s", e)
        return None

    if not check_gstreamer_python_available():
        LOGGER.error("GStreamer Python bindings not available!")
        LOGGER.error("   Install with: sudo apt install python3-gi python3-gst-1.0")
        return None

    try:
        if not rclpy.ok():
            rclpy.init()

        node = GStreamerDepthReceiverNode(
            port=port,
            width=width,
            height=height,
            camera_name=camera_name,
            config_loader=config_loader,
            intrinsics=intrinsics,
        )
        return node

    except Exception as e:
        LOGGER.error(f"Failed to create depth receiver node: {e}")
        import traceback

        traceback.print_exc()
        return None
