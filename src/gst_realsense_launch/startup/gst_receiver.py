#!/usr/bin/env python3
"""RealSense GStreamer Receiver

Receives video streams via GStreamer and manages them using tmux.

Features:
- Video stream reception (depth, color, IR1, IR2)
- Tmux-based stream management
- Optional live visualization
"""

import argparse
import getpass
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gst_realsense_launch.startup.rs_common import CameraIntrinsics, ConfigLoader

try:
    from utils.logger import LOGGER
except ImportError:
    import logging

    LOGGER = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

if TYPE_CHECKING:
    from gst_realsense_launch.startup.gst_depth_receiver_module import GStreamerDepthReceiverNode

try:
    from gst_realsense_launch.startup.gst_depth_receiver_module import (
        GStreamerDepthReceiverNode,
        check_gstreamer_python_available,
        create_depth_receiver_node,
    )

    GST_PYTHON_AVAILABLE = True
except ImportError:
    GST_PYTHON_AVAILABLE = False
    LOGGER.warning("⚠ GStreamer Python bindings not available - depth streaming will be limited")


class DepthMergeProcessor:
    """8-bit depth streams to 16-bit."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.high_byte_buffer: np.ndarray | None = None
        self.low_byte_buffer: np.ndarray | None = None
        self.last_high_time = 0.0
        self.last_low_time = 0.0

    def update_high_byte(self, data: np.ndarray, timestamp: float):
        """high-bit data."""
        self.high_byte_buffer = data
        self.last_high_time = timestamp

    def update_low_byte(self, data: np.ndarray, timestamp: float):
        """low-bit data."""
        self.low_byte_buffer = data
        self.low_time = timestamp

    def get_merged_depth(self) -> np.ndarray | None:
        """Get merged 16-bit depth data."""
        if self.high_byte_buffer is None or self.low_byte_buffer is None:
            return None

        time_diff = abs(self.last_high_time - self.last_low_time)
        if time_diff > 0.1:
            LOGGER.warning(f"Depth high/low byte time mismatch: {time_diff*1000:.1f}ms")

        # merge: depth = (high << 8) | low
        high_shifted = self.high_byte_buffer.astype(np.uint16) << 8
        depth_16bit = high_shifted | self.low_byte_buffer.astype(np.uint16)

        return depth_16bit


class TmuxSessionManager:
    """Manages tmux session for multiple GStreamer receiver pipelines."""

    def __init__(self, session_name: str = "realsense_receiver"):
        self.session_name = session_name
        self.window_count = 0
        self._check_tmux()
        self._cleanup_old_session()
        self._create_session()

    def _check_tmux(self):
        """Check if tmux is available."""
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("tmux is not installed. Please install tmux: sudo apt install tmux")

    def _cleanup_old_session(self):
        """Kill old session if it exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
            LOGGER.info(f"✓ Cleaned up old tmux session '{self.session_name}'")
            time.sleep(0.5)

    def _create_session(self):
        """Create tmux session if it doesn't exist."""
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name],
                check=True,
                capture_output=True,
            )
            LOGGER.info(f"✓ Created tmux session '{self.session_name}'")
        except subprocess.CalledProcessError as e:
            LOGGER.info(f"Warning: Could not create tmux session: {e}")
            raise

    def create_window(self, window_name: str, command: str):
        """Create a new tmux window and run command in it."""
        try:
            subprocess.run(
                ["tmux", "new-window", "-t", self.session_name, "-n", window_name],
                check=True,
                capture_output=True,
                text=True,
            )

            subprocess.run(
                ["tmux", "send-keys", "-t", f"{self.session_name}:{window_name}", command, "C-m"],
                check=True,
                capture_output=True,
            )

            self.window_count += 1
            LOGGER.info(f"  ✓ Created window '{window_name}' in tmux")

        except subprocess.CalledProcessError as e:
            LOGGER.info(f"  ✗ Failed to create window '{window_name}': {e}")
            if e.stderr:
                LOGGER.info(f"     Error output: {e.stderr}")
            raise

    def attach_info(self):
        """Display instructions for attaching to the tmux session."""
        LOGGER.info(f"To view the streams, attach to tmux session:")
        LOGGER.info(f"  tmux attach -t {self.session_name}")
        LOGGER.info(f"Tmux navigation:")
        LOGGER.info(f"  Ctrl+b n : next window")
        LOGGER.info(f"  Ctrl+b p : previous window")
        LOGGER.info(f"  Ctrl+b [0-9] : select window by number")
        LOGGER.info(f"  Ctrl+b d : detach from session")

    def kill_session(self):
        """Kill the entire tmux session and ensure all processes are terminated."""
        if not self._session_exists():
            LOGGER.info(f"Tmux session '{self.session_name}' already terminated")
            return

        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", self.session_name, "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                LOGGER.info(f"Found {len(pids)} processes in tmux session")

                for pid in pids:
                    if pid and pid.isdigit():
                        try:
                            subprocess.run(["kill", "-TERM", pid], timeout=2, check=False)
                            LOGGER.debug(f"  Sent SIGTERM to PID {pid}")
                        except Exception as e:
                            LOGGER.debug(f"  Could not terminate PID {pid}: {e}")

                time.sleep(2)

                for pid in pids:
                    if pid and pid.isdigit():
                        try:
                            check = subprocess.run(
                                ["ps", "-p", pid], capture_output=True, timeout=1, check=False
                            )
                            if check.returncode == 0:
                                subprocess.run(["kill", "-KILL", pid], timeout=1, check=False)
                                LOGGER.debug(f"  Force killed PID {pid}")
                        except Exception as e:
                            LOGGER.debug(f"  Could not check/kill PID {pid}: {e}")

            result = subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                LOGGER.info(f"✓ Killed tmux session '{self.session_name}'")

            time.sleep(0.5)
            if not self._session_exists():
                LOGGER.info("✓ Tmux session cleanup verified")

        except Exception as e:
            LOGGER.error(f"Error during tmux cleanup: {e}")

    def _session_exists(self) -> bool:
        """Check if the tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )
        return result.returncode == 0


class VideoStreamReceiver:
    """Manages video stream reception using tmux and GStreamer."""

    def __init__(
        self,
        camera_name: str,
        config_loader: ConfigLoader,
        show_views: bool = False,
        view_scale: float = 0.5,
    ):
        """Initialize the video stream receiver."""
        self.camera_name = camera_name
        self.config_loader = config_loader
        self.show_views = show_views
        self.view_scale = view_scale

        self.tmux_manager = TmuxSessionManager()
        self._shutdown_event = threading.Event()
        self.depth_merge_processor: DepthMergeProcessor | None = None
        self.depth_receiver_node: Optional["GStreamerDepthReceiverNode"] = (
            None  # For GStreamer Python-based depth receiver
        )
        self.depth_receiver_thread: threading.Thread | None = None

    def start_stream(
        self,
        port: int,
        stream_name: str,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None = None,
    ):
        """Start receiving a video stream and publishing to ROS2."""

        # Special handling for depth streams - use GStreamer Python instead of gscam
        if stream_name == "depth":
            self._start_depth_stream_python(port, encoding, width, height, intrinsics)
            if self.show_views:
                self._start_visualization_in_tmux(port, stream_name, width, height)
            time.sleep(0.5)
            return

        if stream_name == "infra_stereo":
            single_width = width // 2

            self._start_single_stream_in_tmux(
                port,
                "infra1",
                encoding,
                single_width,
                height,
                intrinsics,
                is_y8i=True,
                y8i_width=width,
            )
            if self.show_views:
                self._start_visualization_in_tmux(
                    port, "infra1", single_width, height, is_y8i=True, y8i_width=width
                )
            time.sleep(0.5)

            self._start_single_stream_in_tmux(
                port,
                "infra2",
                encoding,
                single_width,
                height,
                intrinsics,
                is_y8i=True,
                y8i_width=width,
            )
            if self.show_views:
                self._start_visualization_in_tmux(
                    port, "infra2", single_width, height, is_y8i=True, y8i_width=width
                )
            time.sleep(0.5)
        else:
            self._start_single_stream_in_tmux(
                port, stream_name, encoding, width, height, intrinsics
            )
            if self.show_views:
                self._start_visualization_in_tmux(port, stream_name, width, height)
            time.sleep(0.5)

    def _start_single_stream_in_tmux(
        self,
        port: int,
        stream_name: str,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
        is_y8i: bool = False,
        y8i_width: int = None,
    ):
        """Start a single stream receiver in a tmux window (gscam for ROS2)."""

        calib_file = self._get_calibration_file_path(stream_name, width, height)

        if calib_file:
            info_file_url = f"file://{calib_file}"
            tmp_file_path = None
        else:
            LOGGER.info(
                f"  ⚠ No calibration file found for {stream_name} {width}x{height}, using defaults"
            )
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".yaml", prefix=f"{self.camera_name}_{stream_name}_"
            )
            self._create_camera_info_file(tmp_file.name, stream_name, width, height, intrinsics)
            info_file_url = f"file://{tmp_file.name}"
            tmp_file_path = tmp_file.name
            tmp_file.close()

        if is_y8i:
            gst_config = self._build_y8i_pipeline(port, stream_name, y8i_width, height)
        else:
            gst_config = self._build_pipeline(port, stream_name, encoding, width, height)

        image_encodings = self.config_loader.get("receiver.image_encoding", {})

        if stream_name == "depth":
            image_topic = f"/{self.camera_name}/depth/image_rect_raw"
            info_topic = f"/{self.camera_name}/depth/camera_info"
            frame_id = f"{self.camera_name}_depth_optical_frame"
            image_encoding = "16UC1"  # Use 16UC1 (OpenCV format) instead of mono16
        elif stream_name == "color":
            image_topic = f"/{self.camera_name}/color/image_raw"
            info_topic = f"/{self.camera_name}/color/camera_info"
            frame_id = f"{self.camera_name}_color_optical_frame"
            image_encoding = image_encodings.get("color", "rgb8")
        elif stream_name.startswith("infra"):
            image_topic = f"/{self.camera_name}/{stream_name}/image_rect_raw"
            info_topic = f"/{self.camera_name}/{stream_name}/camera_info"
            frame_id = f"{self.camera_name}_{stream_name}_optical_frame"
            image_encoding = image_encodings.get("infra", "mono8")
        else:
            return

        gscam_cmd_parts = [
            "ros2 run gscam gscam_node",
            "--ros-args",
            f"-p gscam_config:='{gst_config}'",
            f"-p camera_name:={self.camera_name}_{stream_name}",
            f"-p camera_info_url:={info_file_url}",
            f"-p frame_id:={frame_id}",
            "-p sync_sink:=false",
        ]

        if image_encoding:
            gscam_cmd_parts.append(f"-p image_encoding:={image_encoding}")

        gscam_cmd_parts.extend(
            [
                f"-r camera/image_raw:={image_topic}",
                f"-r camera/camera_info:={info_topic}",
            ]
        )

        gscam_cmd = " ".join(gscam_cmd_parts)

        if tmp_file_path:
            gscam_cmd = f"trap 'rm -f {tmp_file_path}' EXIT; {gscam_cmd}"

        window_name = f"{stream_name}_{port}"

        LOGGER.info(f"[{stream_name}] Starting on port {port}")
        LOGGER.info(f"  Topic: {image_topic}")
        if stream_name == "depth":
            LOGGER.info(f"  Encoding: {image_encoding} (OpenCV format for 16-bit depth)")
        else:
            LOGGER.info(f"  Encoding: {image_encoding if image_encoding else 'auto-detect'}")

        self.tmux_manager.create_window(window_name, gscam_cmd)

        if stream_name == "depth":
            self._start_depth_conversion_node(port, image_topic, info_topic)

    def _start_depth_stream_python(
        self,
        port: int,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
    ):
        """Start depth stream using GStreamer Python bindings (bypasses gscam).

        This method is used specifically for 16-bit depth streams because
        gscam does not support 16UC1 or mono16 encodings.
        """
        if not GST_PYTHON_AVAILABLE:
            LOGGER.error("❌ Cannot start depth stream: GStreamer Python not available")
            LOGGER.error("   Install with: sudo apt install python3-gi python3-gst-1.0")
            LOGGER.error("   Falling back to gscam (will fail for 16-bit depth)")
            # Fallback to gscam (will likely fail, but at least we try)
            self._start_single_stream_in_tmux(port, "depth", encoding, width, height, intrinsics)
            return

        LOGGER.info(f"[depth] Starting GStreamer Python receiver on port {port}")
        LOGGER.info(f"  Encoding: {encoding}")
        LOGGER.info(f"  Resolution: {width}x{height}")
        LOGGER.info(f"  Topic: /{self.camera_name}/depth/image_rect_raw")
        LOGGER.info(f"  Format: 16UC1 (16-bit depth)")
        LOGGER.info(f"  Method: GStreamer Python (bypassing gscam)")

        # Create the depth receiver node
        self.depth_receiver_node = create_depth_receiver_node(
            port=port,
            width=width,
            height=height,
            camera_name=self.camera_name,
            encoding=encoding,
            config_loader=self.config_loader,
            intrinsics=intrinsics,
        )

        if not self.depth_receiver_node:
            LOGGER.error("❌ Failed to create depth receiver node")
            return

        # Start ROS2 spin in a separate thread
        import rclpy

        def spin_node():
            try:
                rclpy.spin(self.depth_receiver_node)
            except Exception as e:
                LOGGER.error(f"Error in depth receiver node: {e}")

        self.depth_receiver_thread = threading.Thread(target=spin_node, daemon=True)
        self.depth_receiver_thread.start()

        LOGGER.info("✓ Depth receiver node started successfully")

        # Start depth conversion node
        depth_topic = f"/{self.camera_name}/depth/image_rect_raw"
        info_topic = f"/{self.camera_name}/depth/camera_info"
        self._start_depth_conversion_node(port, depth_topic, info_topic)

    def _check_nvdec_available(self) -> bool:
        """Check if NVIDIA hardware decoder is available."""
        try:
            result = subprocess.run(
                ["gst-inspect-1.0", "nvh264dec"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return result.returncode == 0
        except:
            return False

    def _start_depth_conversion_node(self, port: int, depth_topic: str, info_topic: str):
        """Start depth_image_proc convert_metric node for float32 conversion."""

        depth_config = self.config_loader.get("receiver.depth_conversion", {})

        if not depth_config.get("enabled", True):
            LOGGER.info("  Depth conversion disabled in config")
            return

        output_topic = depth_config.get("output_topic", "depth/image")
        output_full_topic = f"/{self.camera_name}/{output_topic}"

        convert_cmd_parts = [
            "ros2 run depth_image_proc convert_metric_node",
            "--ros-args",
            f"-r image_raw:={depth_topic}",
            f"-r camera_info:={info_topic}",
            f"-r image:={output_full_topic}",
        ]

        convert_cmd = " ".join(convert_cmd_parts)
        window_name = f"depth_convert_{port}"

        LOGGER.info(f"  [CONVERT] Starting depth to float32 conversion")
        LOGGER.info(f"    Input: {depth_topic} (16UC1 format)")
        LOGGER.info(f"    Output: {output_full_topic} (32FC1)")

        time.sleep(1.0)
        self.tmux_manager.create_window(window_name, convert_cmd)

    def _build_pipeline(
        self, port: int, stream_name: str, encoding: str, width: int, height: int
    ) -> str:
        """Build GStreamer pipeline for receiving video streams.

        Optimized for NVIDIA hardware encoder compatibility with improved
        format conversion and buffering for gscam.

        Args:
            port: UDP port number
            stream_name: Stream type (depth, color, infra)
            encoding: Codec type (h264, h265, jpeg2000)
            width: Image width
            height: Image height
        """

        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000),
            30000000,  # 30MB - safe for gint
        )

        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        gst_formats = self.config_loader.get("receiver.gstreamer_format", {})

        use_nvdec = self._check_nvdec_available()

        encoding_lower = encoding.lower()

        if stream_name == "depth":
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", False
            )
            drop_str = "true" if drop_on_latency else "false"
            output_format = gst_formats.get("depth", "GRAY16_LE")

            if encoding_lower == "jpeg2000":
                # JPEG2000 for lossless 16-bit depth preservation
                # Z16 → GRAY16_LE → JPEG2000 → GRAY16_LE → auto-detect as 16UC1
                payload = payload_types.get("depth_jpeg2000", 112)

                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG2000,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtpj2kdepay ! openjpegdec "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height},framerate=30/1 "
                    f"! appsink drop=true max-buffers=1"
                )
                LOGGER.info(f"  Using JPEG2000 codec for lossless 16-bit depth")
            elif encoding_lower == "h265":
                # H.265 for depth
                payload = payload_types.get("depth_h265", 113)

                # Select decoder
                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh265dec"
                    LOGGER.info(f"  Using NVIDIA H.265 hardware decoder for {stream_name}")
                else:
                    decoder = f"avdec_h265 max-threads={max_threads}"

                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph265depay ! h265parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height},framerate=30/1 "
                    f"! appsink drop=true max-buffers=1"
                )
            else:
                # H.264 for depth (default)
                payload = payload_types.get("depth_h264", 96)

                # Select decoder - prefer software decoder for better compatibility with gscam
                # NVIDIA decoder can be forced by setting environment variable USE_NVDEC=1
                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh264dec"
                    LOGGER.info(f"  Using NVIDIA hardware decoder for {stream_name}")
                else:
                    decoder = f"avdec_h264 max-threads={max_threads}"

                # Enhanced pipeline with explicit format specs and appsink for gscam
                # The appsink with explicit caps helps gscam auto-detect the correct encoding
                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph264depay ! h264parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height},framerate=30/1 "
                    f"! appsink drop=true max-buffers=1"
                )
        elif stream_name == "color":
            latency = self.config_loader.get("streaming.jitter_buffer.color.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.color.drop_on_latency", False
            )
            drop_str = "true" if drop_on_latency else "false"
            output_format = gst_formats.get("color", "RGB")

            if encoding_lower == "h265":
                payload = payload_types.get("color_h265", 118)

                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh265dec"
                else:
                    decoder = f"avdec_h265 max-threads={max_threads}"

                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph265depay ! h265parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height} "
                )
            else:
                # H.264 for color
                payload = payload_types.get("color_h264", 98)

                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh264dec"
                    LOGGER.info(f"  Using NVIDIA hardware decoder for {stream_name}")
                else:
                    decoder = f"avdec_h264 max-threads={max_threads}"

                # Enhanced pipeline with explicit format specs and better buffering
                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph264depay ! h264parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height} "
                )
        elif stream_name.startswith("infra"):
            latency = self.config_loader.get("streaming.jitter_buffer.infra.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.infra.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            output_format = gst_formats.get("infra", "GRAY8")

            if encoding_lower == "h265":
                payload = payload_types.get("ir_h265", 119)

                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh265dec"
                else:
                    decoder = f"avdec_h265 max-threads={max_threads}"

                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph265depay ! h265parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height} "
                )
            else:
                # H.264 for infrared
                payload = payload_types.get("ir_h264", 97)

                if use_nvdec and os.environ.get("USE_NVDEC", "0") == "1":
                    decoder = "nvh264dec"
                else:
                    decoder = f"avdec_h264 max-threads={max_threads}"

                # Enhanced pipeline with explicit format specs and better buffering
                pipeline = (
                    f"udpsrc port={port} buffer-size={buffer_size} "
                    f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                    f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                    f"! rtph264depay ! h264parse ! {decoder} "
                    f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                    f"! videoconvert n-threads={n_threads} "
                    f"! video/x-raw,format={output_format},width={width},height={height} "
                )
        else:
            return ""

        return pipeline

    def _build_y8i_pipeline(self, port: int, stream_name: str, y8i_width: int, height: int) -> str:
        """Build GStreamer pipeline for Y8I streams."""
        single_width = y8i_width // 2

        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000), 30000000  # 30MB
        )

        latency = self.config_loader.get("streaming.jitter_buffer.infra_stereo.latency", 200)
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.infra_stereo.drop_on_latency", True
        )
        drop_str = "true" if drop_on_latency else "false"
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        payload = payload_types.get("infra_stereo_h264", 99)

        pipeline = (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
            f"! videoconvert n-threads={n_threads} ! video/x-raw,format=GRAY8,width={y8i_width},height={height}"
        )

        if stream_name == "infra1":
            pipeline += f" ! videocrop right={single_width}"
        else:
            pipeline += f" ! videocrop left={single_width}"

        return pipeline

    def _start_visualization_in_tmux(
        self,
        port: int,
        stream_name: str,
        width: int,
        height: int,
        is_y8i: bool = False,
        y8i_width: int = None,
    ):
        """Start a separate visualization window for a stream."""
        display_width = int(width * self.view_scale)
        display_height = int(height * self.view_scale)

        if is_y8i:
            viz_pipeline = self._build_visualization_pipeline_y8i(
                port, stream_name, y8i_width, height, display_width, display_height
            )
        else:
            viz_pipeline = self._build_visualization_pipeline(
                port, stream_name, width, height, display_width, display_height
            )

        window_name = f"viz_{stream_name}_{port}"
        gst_cmd = f"gst-launch-1.0 -v {viz_pipeline}"

        LOGGER.info(f"  [VIZ] Starting visualization for {stream_name}")
        self.tmux_manager.create_window(window_name, gst_cmd)

    def _build_visualization_pipeline(
        self,
        port: int,
        stream_name: str,
        width: int,
        height: int,
        display_width: int,
        display_height: int,
    ) -> str:
        """Build independent visualization pipeline."""

        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000), 30000000  # 30MB
        )

        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        if stream_name == "depth":
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("depth_h264", 96)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        elif stream_name == "color":
            latency = self.config_loader.get("streaming.jitter_buffer.color.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.color.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("color_h264", 98)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        elif stream_name.startswith("infra"):
            latency = self.config_loader.get("streaming.jitter_buffer.infra.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.infra.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("ir_h264", 97)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoscale ! video/x-raw,width={display_width},height={display_height} "
                f"! videoconvert "
                f"! autovideosink sync=false"
            )
        else:
            return ""

        return pipeline

    def _build_visualization_pipeline_y8i(
        self,
        port: int,
        stream_name: str,
        y8i_width: int,
        height: int,
        display_width: int,
        display_height: int,
    ) -> str:
        """Build visualization pipeline for Y8I streams."""
        single_width = y8i_width // 2

        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000), 30000000  # 30MB
        )

        latency = self.config_loader.get("streaming.jitter_buffer.infra_stereo.latency", 200)
        drop_on_latency = self.config_loader.get(
            "streaming.jitter_buffer.infra_stereo.drop_on_latency", True
        )
        drop_str = "true" if drop_on_latency else "false"
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        payload = payload_types.get("infra_stereo_h264", 99)

        pipeline = (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
            f"! videoconvert ! video/x-raw,format=GRAY8,width={y8i_width},height={height}"
        )

        if stream_name == "infra1":
            pipeline += f" ! videocrop right={single_width}"
        else:
            pipeline += f" ! videocrop left={single_width}"

        pipeline += (
            f" ! videoscale ! video/x-raw,width={display_width},height={display_height} "
            f"! videoconvert "
            f"! autovideosink sync=false"
        )

        return pipeline

    def _get_calibration_file_path(self, stream_name: str, width: int, height: int) -> str | None:
        """Get the path to the calibration file for the given stream and resolution."""
        stream_mapping = {
            "depth": "depth",
            "depth_high": "depth_high",
            "depth_low": "depth_low",
            "color": "color",
            "infra1": "infrared",
            "infra2": "infrared",
        }

        stream_prefix = stream_mapping.get(stream_name, stream_name)
        resolution = f"{width}x{height}"

        config_paths = [
            Path("src/config"),
            Path(__file__).parent.parent / "config",
            Path(__file__).parent / "config",
            Path("config"),
            Path.home() / ".config" / "realsense",
        ]

        filename = f"{self.camera_name}_{stream_prefix}_{resolution}.yaml"

        for config_dir in config_paths:
            filepath = config_dir / filename
            if filepath.exists():
                LOGGER.info(f"  ✓ Found calibration file: {filepath}")
                return str(filepath.absolute())

        return None

    def _create_camera_info_file(
        self,
        filepath: str,
        stream_name: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
    ):
        """Create camera_info YAML file."""
        if intrinsics:
            fx, fy = intrinsics.fx, intrinsics.fy
            ppx, ppy = intrinsics.ppx, intrinsics.ppy
            coeffs = intrinsics.distortion
        else:
            intrinsics = self.config_loader.create_default_intrinsics(width, height)
            fx, fy = intrinsics.fx, intrinsics.fy
            ppx, ppy = intrinsics.ppx, intrinsics.ppy
            coeffs = intrinsics.distortion

        content = f"""image_width: {width}
image_height: {height}
camera_name: {self.camera_name}_{stream_name}
camera_matrix:
  rows: 3
  cols: 3
  data: [{fx}, 0.0, {ppx}, 0.0, {fy}, {ppy}, 0.0, 0.0, 1.0]
distortion_model: plumb_bob
distortion_coefficients:
  rows: 1
  cols: 5
  data: [{coeffs[0]}, {coeffs[1]}, {coeffs[2]}, {coeffs[3]}, {coeffs[4]}]
rectification_matrix:
  rows: 3
  cols: 3
  data: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
projection_matrix:
  rows: 3
  cols: 4
  data: [{fx}, 0.0, {ppx}, 0.0, 0.0, {fy}, {ppy}, 0.0, 0.0, 0.0, 1.0, 0.0]
"""
        with open(filepath, "w") as f:
            f.write(content)

    def wait(self):
        """Wait until shutdown is requested."""
        try:
            self.tmux_manager.attach_info()
            LOGGER.info("All receivers running. Press Ctrl+C to stop.")
            self._shutdown_event.wait()
        except KeyboardInterrupt:
            pass

    def kill_gst_launch(
        self, timeout: float = 2.0, include_root: bool = False
    ) -> tuple[list[int], list[int]]:
        """
        Kill all running gst-launch-1.0 processes.
        """
        user = getpass.getuser()

        def list_targets() -> list[int]:
            out = subprocess.check_output(["ps", "-eo", "pid,user,comm"], text=True)
            pids: list[int] = []
            for i, line in enumerate(out.splitlines()):
                if i == 0 or not line.strip():
                    continue
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                pid_str, owner, comm = parts
                if comm == "gst-launch-1.0" and (include_root or owner == user):
                    try:
                        pids.append(int(pid_str))
                    except ValueError:
                        pass
            return pids

        initial = list_targets()
        if not initial:
            return ([], [])

        for pid in initial:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

        time.sleep(timeout)

        remaining = set(list_targets()).intersection(initial)
        for pid in list(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                remaining.discard(pid)
            except PermissionError:
                pass

        killed = [pid for pid in initial if pid not in remaining]
        return (killed, sorted(list(remaining)))

    def stop_all(self):
        """Stop all video receivers and tmux session."""
        self._shutdown_event.set()
        LOGGER.info("=" * 40)
        LOGGER.info("INITIATING SHUTDOWN - Stopping all receivers")
        LOGGER.info("=" * 40)

        try:
            # Stop GStreamer Python depth receiver if running
            if self.depth_receiver_node:
                LOGGER.info("[0/3] Stopping GStreamer Python depth receiver...")
                try:
                    self.depth_receiver_node.stop()
                    self.depth_receiver_node.destroy_node()
                    if self.depth_receiver_thread and self.depth_receiver_thread.is_alive():
                        self.depth_receiver_thread.join(timeout=2)
                    LOGGER.info("  ✓ Depth receiver stopped")
                except Exception as e:
                    LOGGER.error(f"  ✗ Error stopping depth receiver: {e}")

            self.kill_gst_launch()
            if self.tmux_manager:
                LOGGER.info("[1/3] Stopping GStreamer pipelines...")
                self.tmux_manager.kill_session()
                time.sleep(1)

            LOGGER.info("[2/3] Checking for orphaned processes...")
            self._cleanup_orphaned_processes()

            LOGGER.info("[3/3] Final cleanup...")
            time.sleep(0.5)

            LOGGER.info("=" * 40)
            LOGGER.info("✓ SHUTDOWN COMPLETE")
            LOGGER.info("=" * 40)

        except Exception as e:
            LOGGER.error(f"Error during shutdown: {e}")

    def _cleanup_orphaned_processes(self):
        """Clean up any orphaned ROS2/gscam processes."""
        patterns = [
            f"gscam.*{self.camera_name}",
            "depth_image_proc",
            "convert_metric_node",
            "depth_merger_node",
        ]

        for pattern in patterns:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )

                if result.returncode == 0:
                    pids = result.stdout.strip().split("\n")
                    LOGGER.info(f"  Found {len(pids)} orphaned processes matching '{pattern}'")

                    for pid in pids:
                        if pid and pid.isdigit():
                            try:
                                subprocess.run(["kill", "-TERM", pid], timeout=2, check=False)
                                LOGGER.info(f"    ✓ Terminated PID {pid}")
                            except Exception as e:
                                LOGGER.debug(f"    Could not terminate PID {pid}: {e}")

            except Exception as e:
                LOGGER.debug(f"  Error checking for pattern '{pattern}': {e}")

        time.sleep(1)

    def start_depth_split_streams(
        self,
        high_port: int,
        low_port: int,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics,
    ):
        """Start depth split mode reception (8-bit high + 8-bit low = 16-bit depth)."""
        LOGGER.info(f"[depth] Starting SPLIT MODE reception")
        LOGGER.info(f"  High byte port: {high_port}")
        LOGGER.info(f"  Low byte port: {low_port}")
        LOGGER.info(f"  Encoding: {encoding.upper()}")

        # Init merge processor
        self.depth_merge_processor = DepthMergeProcessor(width, height)

        self._start_depth_byte_stream_in_tmux(
            high_port, "depth_high", encoding, width, height, intrinsics
        )

        time.sleep(0.5)

        self._start_depth_byte_stream_in_tmux(
            low_port, "depth_low", encoding, width, height, intrinsics
        )

        time.sleep(0.5)

        #  merger node two 8-bit to 16-bit
        self._start_depth_merger_node(width, height, intrinsics)

    def _start_depth_byte_stream_in_tmux(
        self,
        port: int,
        stream_name: str,
        encoding: str,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None,
    ):
        """啟動單個 8-bit depth stream 接收器 (不發布到 ROS2)."""

        temp_topic = f"/{self.camera_name}/{stream_name}/image_raw"
        temp_info_topic = f"/{self.camera_name}/{stream_name}/camera_info"

        calib_file = self._get_calibration_file_path(stream_name, width, height)

        if calib_file:
            info_file_url = f"file://{calib_file}"
            tmp_file_path = None
        else:
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".yaml", prefix=f"{self.camera_name}_{stream_name}_"
            )
            self._create_camera_info_file(tmp_file.name, stream_name, width, height, intrinsics)
            info_file_url = f"file://{tmp_file.name}"
            tmp_file_path = tmp_file.name
            tmp_file.close()

        gst_config = self._build_depth_split_pipeline(port, stream_name, encoding, width, height)

        frame_id = f"{self.camera_name}_depth_optical_frame"

        gscam_cmd_parts = [
            "ros2 run gscam gscam_node",
            "--ros-args",
            f"-p gscam_config:='{gst_config}'",
            f"-p camera_name:={self.camera_name}_{stream_name}",
            f"-p camera_info_url:={info_file_url}",
            f"-p frame_id:={frame_id}",
            "-p sync_sink:=false",
            "-p image_encoding:=mono8",
            f"-r camera/image_raw:={temp_topic}",
            f"-r camera/camera_info:={temp_info_topic}",
        ]

        gscam_cmd = " ".join(gscam_cmd_parts)

        if tmp_file_path:
            gscam_cmd = f"trap 'rm -f {tmp_file_path}' EXIT; {gscam_cmd}"

        window_name = f"{stream_name}_{port}"

        LOGGER.info(f"[{stream_name}] Starting on port {port}")
        LOGGER.info(f"  Temp topic: {temp_topic}")

        self.tmux_manager.create_window(window_name, gscam_cmd)

    def _build_depth_split_pipeline(
        self, port: int, stream_name: str, encoding: str, width: int, height: int
    ) -> str:
        """depth split pipeline (GRAY8)."""

        buffer_size = min(
            self.config_loader.get("streaming.udp.buffer_size", 30000000), 30000000  # 30MB
        )

        if stream_name == "depth_high":
            latency = self.config_loader.get("streaming.jitter_buffer.depth_high.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth_high.drop_on_latency", False
            )
        elif stream_name == "depth_low":
            latency = self.config_loader.get("streaming.jitter_buffer.depth_low.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth_low.drop_on_latency", False
            )
        else:
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", False
            )

        drop_str = "true" if drop_on_latency else "false"
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")

        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})

        if encoding.lower() == "h264":
            pt_key = f"{stream_name}_h264"
            pt = payload_types.get(pt_key, 114)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={pt}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} "
                f"! video/x-raw,format=GRAY8,width={width},height={height} "
            )
        elif encoding.lower() == "h265":
            pt_key = f"{stream_name}_h265"
            pt = payload_types.get(pt_key, 116)

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload={pt}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph265depay ! h265parse ! avdec_h265 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} "
                f"! video/x-raw,format=GRAY8,width={width},height={height} "
            )
        else:
            pipeline = ""

        return pipeline

    def _start_depth_merger_node(
        self, width: int, height: int, intrinsics: CameraIntrinsics | None
    ):
        """啟動 depth merger node 8-bit to 16-bit ."""

        script_paths = [
            Path(__file__).parent / "depth_merger_node.py",
            Path(__file__).parent.parent / "node" / "depth_merger_node.py",
            Path("src/gst_realsense_launch/node/depth_merger_node.py"),
            Path("depth_merger_node.py"),
        ]

        script_path = None
        for path in script_paths:
            if path.exists():
                script_path = path
                break

        if not script_path:
            LOGGER.error("  ✗ Could not find depth_merger_node.py")
            LOGGER.error(
                "  Please ensure depth_merger_node.py is in the same directory as gst_receiver.py"
            )
            return

        merger_cmd_parts = [
            "python3",
            str(script_path.absolute()),
            "--ros-args",
            f"-p camera_name:={self.camera_name}",
            f"-p width:={width}",
            f"-p height:={height}",
        ]

        merger_cmd = " ".join(merger_cmd_parts)
        window_name = "depth_merger"

        LOGGER.info(f"  [MERGER] Starting depth merger node")
        LOGGER.info(
            f"    Input: /{self.camera_name}/depth_high/image_raw, /{self.camera_name}/depth_low/image_raw"
        )
        LOGGER.info(f"    Output: /{self.camera_name}/depth/image_rect_raw (mono16)")

        self.tmux_manager.create_window(window_name, merger_cmd)

        time.sleep(1.0)
        depth_topic = f"/{self.camera_name}/depth/image_rect_raw"
        info_topic = f"/{self.camera_name}/depth/camera_info"
        self._start_depth_conversion_node(0, depth_topic, info_topic)


def check_and_cleanup_existing_resources(camera_name: str):
    """Check for and clean up any existing resources before starting."""
    LOGGER.info("Checking for existing resources...")

    issues_found = False

    result = subprocess.run(
        ["tmux", "has-session", "-t", "realsense_receiver"], capture_output=True, text=True
    )
    if result.returncode == 0:
        LOGGER.warning("  ⚠ Found existing tmux session 'realsense_receiver'")
        issues_found = True
        subprocess.run(["tmux", "kill-session", "-t", "realsense_receiver"])
        LOGGER.info("    ✓ Cleaned up existing session")
        time.sleep(1)

    patterns = [
        f"gscam.*{camera_name}",
        "depth_image_proc",
        "convert_metric_node",
        "depth_merger_node",
    ]

    for pattern in patterns:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            LOGGER.warning(f"  ⚠ Found {len(pids)} orphaned processes matching '{pattern}'")
            issues_found = True

            for pid in pids:
                if pid and pid.isdigit():
                    try:
                        subprocess.run(["kill", "-TERM", pid], timeout=1)
                        LOGGER.info(f"    ✓ Terminated PID {pid}")
                    except:
                        pass

    if issues_found:
        LOGGER.info("  Waiting for cleanup to complete...")
        time.sleep(2)
        LOGGER.info("✓ Cleanup complete")
    else:
        LOGGER.info("✓ No existing resources found")

    return True


def signal_handler(signum, frame):
    LOGGER.info(f"⚠ Received signal {signum}")
    sys.exit(0)


def main():
    """Run the RealSense GStreamer receiver."""
    parser = argparse.ArgumentParser(description="RealSense GStreamer Stream Receiver")
    parser.add_argument("--config", default="src/config/config.yaml", help="Configuration file")
    parser.add_argument(
        "--preset",
        choices=["d435i", "d455", "d415", "l515"],
        help="Use camera preset",
    )
    parser.add_argument("--base-port", type=int, help="Override base port")
    parser.add_argument("--camera-name", help="Override camera name")
    parser.add_argument("--show-views", action="store_true", help="Display received video streams")
    parser.add_argument("--view-scale", type=float, help="Display window scale factor")
    parser.add_argument("--width", type=int, help="Image width")
    parser.add_argument("--height", type=int, help="Image height")

    args = parser.parse_args()

    try:
        config_loader = ConfigLoader(args.config)

        network_cfg = config_loader.get_network_config(args)
        camera_cfg = config_loader.get_camera_config(args)
        receiver_cfg = config_loader.get_receiver_config(args)

        check_and_cleanup_existing_resources(camera_cfg["camera_name"])

        width = args.width or 640
        height = args.height or 480

        depth_mode = config_loader.get("encoding.depth.mode", "legacy")
        depth_codec = config_loader.get("encoding.depth.codec", "h264")

        video_receiver = VideoStreamReceiver(
            camera_name=camera_cfg["camera_name"],
            config_loader=config_loader,
            show_views=receiver_cfg.get("show_views", False),
            view_scale=receiver_cfg.get("view_scale", 0.5),
        )

        if args.preset:
            preset = config_loader.get_preset(args.preset)
            if preset:
                streams = preset["streams"]
                ports = []
                stream_names = []
                encodings = []
                widths = []
                heights = []

                for s in streams:
                    if s["name"] == "depth":
                        port = network_cfg["base_port"] + s["port_offset"]

                        if depth_mode == "split":
                            LOGGER.info(f"Using DEPTH SPLIT mode")
                            high_port = config_loader.get_port_for_stream("depth_high", port + 1)
                            low_port = config_loader.get_port_for_stream("depth_low", port + 2)

                            intrinsics = config_loader.create_default_intrinsics(width, height)
                            video_receiver.start_depth_split_streams(
                                high_port,
                                low_port,
                                depth_codec,
                                width,
                                height,
                                intrinsics,
                            )
                        else:
                            LOGGER.info(f"Using DEPTH LEGACY mode")
                            intrinsics = config_loader.create_default_intrinsics(width, height)
                            video_receiver.start_stream(
                                port, "depth", s["encoding"], width, height, intrinsics
                            )
                    else:
                        port = network_cfg["base_port"] + s["port_offset"]
                        ports.append(port)
                        stream_names.append(s["name"])
                        encodings.append(s["encoding"])

                        if s["name"] == "infra_stereo":
                            widths.append(width * 2)
                        else:
                            widths.append(width)
                        heights.append(height)

                LOGGER.info(f"Using {args.preset.upper()} preset")
            else:
                LOGGER.info(f"Error: Preset {args.preset} not found")
                sys.exit(1)
        else:
            LOGGER.info("Error: --preset required (d435i, d455, d415, l515)")
            sys.exit(1)

        LOGGER.info(f"{'='*40}")
        LOGGER.info("REALSENSE GSTREAMER RECEIVER")
        LOGGER.info(f"{'='*40}")
        LOGGER.info(f"Camera Name: {camera_cfg['camera_name']}")
        LOGGER.info("Video Streams:")
        for port, stream, enc in zip(ports, stream_names, encodings, strict=False):
            LOGGER.info(f"  {stream:10s} - Port {port} ({enc.upper()})")
        LOGGER.info(f"{'='*40}")

        for port, stream, encoding, stream_width, stream_height in zip(
            ports, stream_names, encodings, widths, heights, strict=False
        ):
            intrinsics = config_loader.create_default_intrinsics(
                stream_width if stream != "infra_stereo" else stream_width // 2, stream_height
            )
            video_receiver.start_stream(
                port, stream, encoding, stream_width, stream_height, intrinsics
            )

        time.sleep(1)
        LOGGER.info(f"{'='*40}")
        LOGGER.info("ALL RECEIVERS STARTED")
        LOGGER.info(f"{'='*40}")

        video_receiver.wait()

    except Exception as e:
        LOGGER.error(f"✗ Fatal error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        LOGGER.info("Starting cleanup sequence...")
        if "video_receiver" in locals():
            try:
                video_receiver.stop_all()
            except Exception as e:
                LOGGER.error(f"Error stopping video receiver: {e}")

        LOGGER.info("=" * 40)
        LOGGER.info("✓ ALL CLEANUP COMPLETE")
        LOGGER.info("=" * 40 + "")


if __name__ == "__main__":

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
