#!/usr/bin/env python3
"""RealSense GStreamer Receiver (Pure GStreamer - No ROS2)

Receives video streams via GStreamer and manages them using tmux.
All ROS2 functionality is handled by the launch file.

Features:
- Video stream reception (depth, color, IR1, IR2)
- Tmux-based stream management
- Optional live visualization
"""

import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from gst_realsense_launch.rs_common import CameraIntrinsics, ConfigLoader
from utils.logger import LOGGER


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
            gst_config = self._build_pipeline(port, stream_name, width, height)

        image_encodings = self.config_loader.get("receiver.image_encoding", {})

        if stream_name == "depth":
            image_topic = f"/{self.camera_name}/depth/image_rect_raw"
            info_topic = f"/{self.camera_name}/depth/camera_info"
            frame_id = f"{self.camera_name}_depth_optical_frame"
            image_encoding = None
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
        LOGGER.info(f"  Encoding: {image_encoding if image_encoding else 'auto-detect (mono16)'}")

        self.tmux_manager.create_window(window_name, gscam_cmd)

        if stream_name == "depth":
            self._start_depth_conversion_node(port, image_topic, info_topic)

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
        LOGGER.info(f"    Input: {depth_topic} (mono16)")
        LOGGER.info(f"    Output: {output_full_topic} (32FC1)")

        time.sleep(0.5)
        self.tmux_manager.create_window(window_name, convert_cmd)

    def _build_pipeline(self, port: int, stream_name: str, width: int, height: int) -> str:
        """Build GStreamer pipeline for standard streams."""

        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
        max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
        leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        payload_types = self.config_loader.get("streaming.rtp.payload_types", {})
        gst_formats = self.config_loader.get("receiver.gstreamer_format", {})

        if stream_name == "depth":
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("depth_h264", 96)
            output_format = gst_formats.get("depth", "GRAY16_LE")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        elif stream_name == "color":
            latency = self.config_loader.get("streaming.jitter_buffer.color.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.color.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("color_h264", 98)
            output_format = gst_formats.get("color", "RGB")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        elif stream_name.startswith("infra"):
            latency = self.config_loader.get("streaming.jitter_buffer.infra.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.infra.drop_on_latency", True
            )
            drop_str = "true" if drop_on_latency else "false"
            payload = payload_types.get("ir_h264", 97)
            output_format = gst_formats.get("infra", "GRAY8")

            pipeline = (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format={output_format}"
            )
        else:
            return ""

        return pipeline

    def _build_y8i_pipeline(self, port: int, stream_name: str, y8i_width: int, height: int) -> str:
        """Build GStreamer pipeline for Y8I streams."""
        single_width = y8i_width // 2

        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
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

        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
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
        """Build independent visualization pipeline for Y8I streams."""
        single_width = y8i_width // 2

        buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
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
            "color": "color",
            "infra1": "infrared",
            "infra2": "infrared",
        }

        stream_prefix = stream_mapping.get(stream_name, stream_name)
        resolution = f"{width}x{height}"

        config_paths = [
            Path("src/config"),
            Path(__file__).parent.parent / "config",
            Path.home() / ".config" / "realsense",
        ]

        filename = f"{stream_prefix}_camera_{resolution}.yaml"

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

    def stop_all(self):
        """Stop all video receivers and tmux session."""
        self._shutdown_event.set()
        LOGGER.info("=" * 40)
        LOGGER.info("INITIATING SHUTDOWN - Stopping all receivers")
        LOGGER.info("=" * 40)

        try:
            if self.tmux_manager:
                LOGGER.info("[1/2] Stopping GStreamer pipelines...")
                self.tmux_manager.kill_session()
                time.sleep(1)

            LOGGER.info("[2/2] Checking for orphaned processes...")
            self._cleanup_orphaned_processes()

        except Exception as e:
            LOGGER.error(f"Error during cleanup: {e}")

        LOGGER.info("=" * 40)
        LOGGER.info("✓ SHUTDOWN COMPLETE")
        LOGGER.info("=" * 40)

    def _cleanup_orphaned_processes(self):
        """Clean up any orphaned gscam or depth_image_proc processes."""
        process_patterns = [
            f"gscam.*{self.camera_name}",
            f"depth_image_proc.*{self.camera_name}",
            "convert_metric_node",
        ]

        for pattern in process_patterns:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )

                if result.returncode == 0 and result.stdout:
                    pids = result.stdout.strip().split("\n")
                    for pid in pids:
                        if pid and pid.isdigit():
                            try:
                                subprocess.run(["kill", "-TERM", pid], timeout=1, check=False)
                                LOGGER.info(f"  Terminated orphaned process (PID {pid})")
                            except Exception as e:
                                LOGGER.debug(f"  Could not kill PID {pid}: {e}")
            except Exception as e:
                LOGGER.debug(f"  Error checking pattern '{pattern}': {e}")


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

        video_receiver = VideoStreamReceiver(
            camera_name=camera_cfg["camera_name"],
            config_loader=config_loader,
            show_views=receiver_cfg.get("show_views", False),
            view_scale=receiver_cfg.get("view_scale", 0.5),
        )

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
    import signal

    def signal_handler(signum, frame):
        LOGGER.info(f"⚠ Received signal {signum}")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
