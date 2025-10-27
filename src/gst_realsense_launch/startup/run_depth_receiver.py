#!/usr/bin/env python3
"""RealSense GStreamer Receiver - Simplified for H.264 Depth Streaming

Receives depth video streams via GStreamer H.264 and manages them using tmux.
Optimized for: Z16 → GRAY16_LE → H.264 → GRAY16_LE → ROS2 16UC1

Features:
- H.264 depth stream reception (removed JPEG2000 and H.265)
- Tmux-based stream management
- Optional live visualization
- Direct 16-bit depth support via GStreamer Python
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

from rs_common import CameraIntrinsics, ConfigLoader

try:
    from utils.logger import LOGGER
except ImportError:
    import logging

    LOGGER = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

if TYPE_CHECKING:
    from gst_depth_receiver_module import GStreamerDepthReceiverNode

try:
    from gst_depth_receiver_module import (
        GStreamerDepthReceiverNode,
        create_depth_receiver_node,
    )

    GST_PYTHON_AVAILABLE = True
except ImportError:
    GST_PYTHON_AVAILABLE = False
    LOGGER.warning("GStreamer Python bindings not available - depth streaming will be limited")


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
    """Manages video stream reception using tmux and GStreamer - Simplified for H.264 Depth."""

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

    def start_depth_stream(
        self,
        port: int,
        width: int,
        height: int,
        intrinsics: CameraIntrinsics | None = None,
    ):
        """Start receiving H.264 depth stream and publishing to ROS2.

        This method uses GStreamer Python bindings to receive 16-bit depth directly,
        bypassing gscam limitations.

        Pipeline: UDP → RTP → H.264 decode → GRAY16_LE → ROS2 16UC1
        """
        LOGGER.info("=" * 50)
        LOGGER.info("STARTING H.264 DEPTH STREAM RECEIVER")
        LOGGER.info("=" * 50)

        if not GST_PYTHON_AVAILABLE:
            LOGGER.error("❌ Cannot start depth stream: GStreamer Python not available")
            LOGGER.error("   Install with: sudo apt install python3-gi python3-gst-1.0")
            return

        LOGGER.info(f"[depth] Port: {port}")
        LOGGER.info(f"[depth] Resolution: {width}x{height}")
        LOGGER.info(f"[depth] Encoding: H.264")
        LOGGER.info(f"[depth] Topic: /{self.camera_name}/depth/image_rect_raw")
        LOGGER.info(f"[depth] Format: 16UC1 (16-bit depth)")
        LOGGER.info(f"[depth] Method: GStreamer Python (direct 16-bit)")

        # Get the path to the standalone depth receiver script
        script_dir = Path(__file__).resolve().parent
        depth_receiver_script = script_dir / "run_depth_receiver.py"

        # Check if script exists
        if not depth_receiver_script.exists():
            LOGGER.error(f"❌ Depth receiver script not found: {depth_receiver_script}")
            LOGGER.error(
                "   Make sure run_depth_receiver.py is in the same directory as gst_receiver.py"
            )
            return

        # Build command with all necessary arguments
        cmd_parts = [
            f"python3 {depth_receiver_script}",
            f"--port {port}",
            f"--width {width}",
            f"--height {height}",
            f"--camera-name {self.camera_name}",
            "--encoding h264",  # Force H.264
        ]

        # Add config file path if available
        if hasattr(self.config_loader, "config_file"):
            cmd_parts.append(f"--config {self.config_loader.config_file}")

        # Add intrinsics if provided
        if intrinsics:
            cmd_parts.extend(
                [
                    f"--fx {intrinsics.fx}",
                    f"--fy {intrinsics.fy}",
                    f"--ppx {intrinsics.ppx}",
                    f"--ppy {intrinsics.ppy}",
                ]
            )
            if intrinsics.distortion:
                distortion_str = " ".join(str(d) for d in intrinsics.distortion)
                cmd_parts.append(f"--distortion {distortion_str}")

        # Build final command
        depth_cmd = " ".join(cmd_parts)
        window_name = f"depth_{port}"

        LOGGER.info(f"  Creating tmux window: {window_name}")
        LOGGER.debug(f"  Command: {depth_cmd}")

        # Create tmux window and run the depth receiver
        try:
            self.tmux_manager.create_window(window_name, depth_cmd)
            LOGGER.info("✓ Depth receiver started in tmux")
        except Exception as e:
            LOGGER.error(f"❌ Failed to start depth receiver in tmux: {e}")
            return

        # Wait a moment for the node to initialize
        time.sleep(1.0)

        # Start depth conversion node (converts 16UC1 → 32FC1 for navigation)
        depth_topic = f"/{self.camera_name}/depth/image_rect_raw"
        info_topic = f"/{self.camera_name}/depth/camera_info"
        self._start_depth_conversion_node(port, depth_topic, info_topic)

        LOGGER.info("✓ Depth stream setup complete")

        # Optional visualization
        if self.show_views:
            self._start_depth_visualization(port, width, height)

    def _start_depth_conversion_node(self, port: int, depth_topic: str, info_topic: str):
        """Start depth_image_proc convert_metric node for float32 conversion.

        Converts: 16UC1 (millimeters) → 32FC1 (meters)
        This is required for navigation stack compatibility.
        """
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
        LOGGER.info(f"    Input: {depth_topic} (16UC1 millimeters)")
        LOGGER.info(f"    Output: {output_full_topic} (32FC1 meters)")

        time.sleep(1.0)
        self.tmux_manager.create_window(window_name, convert_cmd)

    def _start_depth_visualization(self, port: int, width: int, height: int):
        """Start visualization for depth stream using GStreamer."""
        display_width = int(width * self.view_scale)
        display_height = int(height * self.view_scale)

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

        # Build visualization pipeline (H.264 only)
        viz_pipeline = (
            f"gst-launch-1.0 -e "
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}" '
            f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert "
            f"! videoscale "
            f"! video/x-raw,width={display_width},height={display_height} "
            f"! autovideosink sync=false"
        )

        window_name = f"view_depth_{port}"
        LOGGER.info(f"  [VISUALIZATION] Starting depth visualization window")
        LOGGER.info(f"    Display size: {display_width}x{display_height}")

        self.tmux_manager.create_window(window_name, viz_pipeline)

    def stop_all(self):
        """Stop all streams and cleanup."""
        LOGGER.info("Stopping all streams...")
        self._shutdown_event.set()

        # Kill tmux session (this will terminate all GStreamer pipelines)
        self.tmux_manager.kill_session()

        LOGGER.info("✓ All streams stopped")

    def wait(self):
        """Wait for shutdown signal."""
        try:
            while not self._shutdown_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            LOGGER.info("Received interrupt signal")


def check_and_cleanup_existing_resources(camera_name: str):
    """Check for and clean up any existing resources before starting."""
    LOGGER.info("Checking for existing resources...")

    issues_found = False

    result = subprocess.run(
        ["tmux", "has-session", "-t", "realsense_receiver"], capture_output=True, text=True
    )
    if result.returncode == 0:
        LOGGER.warning("Found existing tmux session 'realsense_receiver'")
        issues_found = True
        subprocess.run(["tmux", "kill-session", "-t", "realsense_receiver"])
        LOGGER.info("    ✓ Cleaned up existing session")
        time.sleep(1)

    patterns = [
        f"run_depth_receiver.*{camera_name}",
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
            LOGGER.warning(f"Found {len(pids)} orphaned processes matching '{pattern}'")
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
    LOGGER.warning(f"Received signal {signum}")
    sys.exit(0)


def main():
    """Run the RealSense GStreamer receiver - H.264 Depth optimized."""
    parser = argparse.ArgumentParser(description="RealSense GStreamer Depth Receiver (H.264 only)")
    parser.add_argument("--config", default="src/config/config.yaml", help="Configuration file")
    parser.add_argument("--port", type=int, required=True, help="UDP port for depth stream")
    parser.add_argument("--camera-name", default="camera", help="Camera name for ROS2 topics")
    parser.add_argument("--show-views", action="store_true", help="Display received video stream")
    parser.add_argument("--view-scale", type=float, default=0.5, help="Display window scale factor")
    parser.add_argument("--width", type=int, default=640, help="Image width")
    parser.add_argument("--height", type=int, default=480, help="Image height")

    args = parser.parse_args()

    try:
        config_loader = ConfigLoader(args.config)

        check_and_cleanup_existing_resources(args.camera_name)

        # Create intrinsics
        intrinsics = config_loader.create_default_intrinsics(args.width, args.height)

        # Create receiver
        video_receiver = VideoStreamReceiver(
            camera_name=args.camera_name,
            config_loader=config_loader,
            show_views=args.show_views,
            view_scale=args.view_scale,
        )

        LOGGER.info(f"{'='*50}")
        LOGGER.info("REALSENSE DEPTH RECEIVER (H.264 ONLY)")
        LOGGER.info(f"{'='*50}")
        LOGGER.info(f"Camera: {args.camera_name}")
        LOGGER.info(f"Port: {args.port}")
        LOGGER.info(f"Resolution: {args.width}x{args.height}")
        LOGGER.info(f"Encoding: H.264")
        LOGGER.info(f"{'='*50}")

        # Start depth stream
        video_receiver.start_depth_stream(
            port=args.port,
            width=args.width,
            height=args.height,
            intrinsics=intrinsics,
        )

        time.sleep(1)
        LOGGER.info(f"{'='*50}")
        LOGGER.info("DEPTH RECEIVER STARTED")
        LOGGER.info(f"{'='*50}")

        # Show tmux attach instructions
        video_receiver.tmux_manager.attach_info()

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

        LOGGER.info("=" * 50)
        LOGGER.info("✓ CLEANUP COMPLETE")
        LOGGER.info("=" * 50)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
