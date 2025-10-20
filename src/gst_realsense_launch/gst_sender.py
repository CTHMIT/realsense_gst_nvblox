#!/usr/bin/env python3
"""
RealSense Multi-Stream Sender with tmux Integration

Modified to run each GStreamer pipeline in a separate tmux window for better
visibility and debugging capabilities.
"""

import argparse
import atexit
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from gst_realsense_launch.rs_common import (
    FOURCC_COLOR,
    FOURCC_DEPTH,
    FOURCC_IR,
    FOURCC_IR_STEREO,
    ConfigLoader,
    StreamConfig,
    format_stream_label,
    get_stream_type_from_fourcc,
    parse_resolution,
    validate_network_config,
)
from gst_realsense_launch.rs_core import EncoderFactory, StreamStrategyFactory
from utils.logger import LOGGER

try:
    import pyrealsense2 as rs
except ImportError:
    rs = None
    LOGGER.warning("pyrealsense2 not available. Hardware IMU streaming disabled.")


@dataclass
class Mode:
    """Represents a video mode with resolution and frame rates."""

    fourcc: str
    size: tuple[int, int]
    fps_list: list[int]


@dataclass
class DeviceInfo:
    """Information about detected RealSense device."""

    dev: str
    card: str
    model: str
    serial: str | None
    modes: list[Mode]


class RealSenseDetector:
    """
    Detects and probes RealSense cameras using V4L2.
    """

    MODEL_PATTERNS = {
        r"435i": "D435i",
        r"D435I": "D435i",
        r"D435": "D435",
        r"455": "D455",
        r"D455": "D455",
        r"415": "D415",
        r"D415": "D415",
        r"L515": "L515",
        r"SR300": "SR300",
    }

    _rs_serial_cache = None

    @staticmethod
    def list_video_nodes() -> list[str]:
        """List all /dev/videoX nodes."""
        return [
            os.path.join("/dev", x)
            for x in sorted(os.listdir("/dev"))
            if x.startswith("video") and x[5:].isdigit()
        ]

    @staticmethod
    def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
        """Run shell command and return result."""
        return subprocess.run(cmd, capture_output=True, text=True)

    @classmethod
    def _get_rs_serial_mapping(cls) -> dict[str, str]:
        if cls._rs_serial_cache is not None:
            return cls._rs_serial_cache

        mapping: dict = {}

        if not rs:
            cls._rs_serial_cache = mapping
            return mapping

        try:
            ctx = rs.context()
            devices = ctx.query_devices()

            for dev in devices:
                serial = dev.get_info(rs.camera_info.serial_number)
                mapping[serial] = serial
                name = dev.get_info(rs.camera_info.name)
                mapping[name] = serial

        except Exception as e:
            LOGGER.error(f"Could not query RealSense devices via SDK: {e}")

        cls._rs_serial_cache = mapping
        return mapping

    @classmethod
    def extract_model(cls, card_name: str, dev: str | None = None) -> str:
        text = card_name or ""
        if dev:
            try:
                props = cls.run_cmd(["udevadm", "info", "--query=property", f"--name={dev}"]).stdout
                for key in ("ID_V4L_PRODUCT", "ID_MODEL", "ID_MODEL_FROM_DATABASE"):
                    for line in props.splitlines():
                        if line.startswith(f"{key}="):
                            text += " " + line.split("=", 1)[1]
            except Exception:
                LOGGER.warning(f"Could not get udev properties for {dev}")

        for pattern, model in cls.MODEL_PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                return model

        return "Unknown"

    @classmethod
    def extract_serial(cls, dev: str, card_name: str = "") -> str | None:
        if rs:
            try:
                ctx = rs.context()
                devices = ctx.query_devices()

                try:
                    result = cls.run_cmd(["udevadm", "info", "--query=property", f"--name={dev}"])
                    usb_path = None
                    id_path = None

                    for line in result.stdout.splitlines():
                        if line.startswith("ID_PATH="):
                            id_path = line.split("=", 1)[1].strip()
                        elif line.startswith("DEVPATH="):
                            usb_path = line.split("=", 1)[1].strip()

                    if id_path or usb_path:
                        if len(devices) == 1:
                            return devices[0].get_info(rs.camera_info.serial_number)

                        LOGGER.info(
                            "Multiple RealSense devices detected. Using first device serial."
                        )
                        return devices[0].get_info(rs.camera_info.serial_number)

                except Exception as e:
                    LOGGER.error(f"Could not get USB path for {dev}: {e}")

            except Exception as e:
                LOGGER.error(f"Could not query RealSense SDK for serial: {e}")

        try:
            result = cls.run_cmd(["udevadm", "info", "--query=property", f"--name={dev}"])
            for line in result.stdout.splitlines():
                if "ID_SERIAL_SHORT=" in line:
                    return line.split("=", 1)[1].strip()
        except Exception:
            LOGGER.warning(f"Could not get serial from udev for {dev}")

        return None

    @classmethod
    def probe_device(cls, dev: str) -> DeviceInfo | None:
        try:
            all_out = cls.run_cmd(["v4l2-ctl", "-d", dev, "--all"]).stdout
            card = ""
            for line in all_out.splitlines():
                if line.strip().startswith("Card type"):
                    card = line.split(":", 1)[1].strip()
                    break

            if not ("RealSense" in card or "Intel(R) RealSense" in card):
                return None

            model = cls.extract_model(card, dev)
            serial = cls.extract_serial(dev, card)

            fmts = cls.run_cmd(["v4l2-ctl", "-d", dev, "--list-formats-ext"]).stdout
            modes = cls._parse_formats(fmts)

            if not modes:
                return None

            return DeviceInfo(dev=dev, card=card, model=model, serial=serial, modes=modes)

        except Exception as e:
            LOGGER.warning(f"Failed to probe {dev}: {e}")
            return None

    @staticmethod
    def _parse_formats(fmts: str) -> list[Mode]:
        modes: list[Mode] = []
        current_fourcc = None
        size_wh: tuple[int, int] | None = None
        fps_accum: list[float] = []

        fmt_re = re.compile(r"\[\d+\]: '(.{4})' ")
        size_re = re.compile(r"Size:\s+Discrete\s+(\d+)x(\d+)")
        fps_re = re.compile(r"\((\d+\.\d+|\d+) fps\)")

        def flush_pending():
            nonlocal modes, current_fourcc, size_wh, fps_accum
            if current_fourcc and size_wh and fps_accum:
                dedup_fps = sorted({int(round(f)) for f in fps_accum}, reverse=True)
                modes.append(Mode(current_fourcc, size_wh, dedup_fps))
            size_wh = None
            fps_accum = []

        for line in fmts.splitlines():
            m_fmt = fmt_re.search(line)
            if m_fmt:
                flush_pending()
                current_fourcc = m_fmt.group(1).strip()
                continue

            m_size = size_re.search(line)
            if m_size:
                flush_pending()
                size_wh = (int(m_size.group(1)), int(m_size.group(2)))
                continue

            m_fps = fps_re.search(line)
            if m_fps:
                fps_accum.append(float(m_fps.group(1)))

        flush_pending()
        return modes

    @classmethod
    def detect_all_cameras(cls) -> list[DeviceInfo]:
        cameras = []
        for dev in cls.list_video_nodes():
            info = cls.probe_device(dev)
            if info:
                cameras.append(info)
        return cameras

    @staticmethod
    def group_by_serial(cameras: list[DeviceInfo]) -> dict:
        grouped: dict = {}
        for cam in cameras:
            serial = cam.serial or "unknown"
            if serial not in grouped:
                grouped[serial] = []
            grouped[serial].append(cam)
        return grouped


class IMUSender:
    """Sends IMU data from RealSense camera over UDP."""

    def __init__(self, serial: str, host: str, port: int):
        if not rs:
            raise RuntimeError("pyrealsense2 not available")

        self.serial = serial
        self.host = host
        self.port = port
        self.running: bool = False
        self.thread: threading.Thread | None = None

        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_device(serial)
        self.config.enable_stream(rs.stream.accel)
        self.config.enable_stream(rs.stream.gyro)

        import socket

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._stream_loop, daemon=True)
        self.thread.start()
        LOGGER.info(f"✓ IMU streaming started for {self.serial}")

    def _stream_loop(self):
        pipeline_started = False
        try:
            ctx = rs.context()
            devices = ctx.query_devices()

            device_found = False
            for dev in devices:
                if dev.get_info(rs.camera_info.serial_number) == self.serial:
                    device_found = True
                    LOGGER.info(f"  Found IMU device: {dev.get_info(rs.camera_info.name)}")
                    break

            if not device_found:
                raise RuntimeError(f"Device {self.serial} not connected")

            self.pipeline.start(self.config)
            pipeline_started = True

            while self.running:
                try:
                    frames = self.pipeline.wait_for_frames(timeout_ms=1000)

                    accel_frame = frames.first_or_default(rs.stream.accel)
                    gyro_frame = frames.first_or_default(rs.stream.gyro)

                    if accel_frame and gyro_frame:
                        accel = accel_frame.as_motion_frame().get_motion_data()
                        gyro = gyro_frame.as_motion_frame().get_motion_data()

                        imu_data = {
                            "type": "imu",
                            "timestamp": time.time(),
                            "serial": self.serial,
                            "accel": {"x": accel.x, "y": accel.y, "z": accel.z},
                            "gyro": {"x": gyro.x, "y": gyro.y, "z": gyro.z},
                        }

                        data = json.dumps(imu_data).encode("utf-8")
                        self.sock.sendto(data, (self.host, self.port))

                except RuntimeError as e:
                    if "didn't arrive" in str(e):
                        continue
                    LOGGER.warning(f"IMU runtime error: {e}")
                    raise

        except Exception as e:
            LOGGER.exception(f"IMU error for {self.serial}: {e}")
        finally:
            if pipeline_started:
                try:
                    self.pipeline.stop()
                except Exception as e:
                    LOGGER.warning(f"Error stopping IMU pipeline: {e}")
            self.sock.close()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        try:
            self.sock.close()
        except Exception:
            pass


class TmuxSessionManager:
    """Manages tmux session for multiple GStreamer pipelines."""

    def __init__(self, session_name: str = "realsense_streams"):
        self.session_name = session_name
        self.window_count = 0
        self._check_tmux()
        self._create_session()

    def _check_tmux(self):
        """Check if tmux is available."""
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise RuntimeError("tmux is not installed. Please install tmux: sudo apt install tmux")

    def _create_session(self):
        """Create tmux session, cleaning up any existing session first."""
        # Check if session already exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )

        if result.returncode == 0:
            # Session exists, kill it first
            LOGGER.warning(f"⚠️  Found existing tmux session '{self.session_name}', cleaning up...")
            kill_result = subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name], capture_output=True
            )
            if kill_result.returncode == 0:
                LOGGER.info(f"✓ Cleaned up existing tmux session '{self.session_name}'")
                time.sleep(0.5)  # Give tmux time to fully cleanup
            else:
                LOGGER.warning(f"Could not kill existing session (it may have already closed)")

        # Create new session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session_name, "-n", "control"], check=True
        )
        LOGGER.info(f"✓ Created tmux session '{self.session_name}'")

    def create_window(self, window_name: str, command: str):
        """Create a new tmux window and run command in it."""
        self.window_count += 1

        subprocess.run(
            [
                "tmux",
                "new-window",
                "-t",
                f"{self.session_name}:{self.window_count}",
                "-n",
                window_name,
            ],
            check=True,
        )

        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{self.session_name}:{window_name}",
                command,
                "C-m",
            ],
            check=True,
        )

        LOGGER.info(f"  ✓ Created window '{window_name}' in tmux")

    def attach(self):
        """Display instructions for attaching to the tmux session."""
        LOGGER.info(f"{'='*40}")
        LOGGER.info("To view the streams, attach to tmux session:")
        LOGGER.info(f"  tmux attach -t {self.session_name}")
        LOGGER.info("Tmux navigation:")
        LOGGER.info("  Ctrl+b n : next window")
        LOGGER.info("  Ctrl+b p : previous window")
        LOGGER.info("  Ctrl+b [0-9] : select window by number")
        LOGGER.info("  Ctrl+b d : detach from session")
        LOGGER.info("  Ctrl+b & : kill current window")
        LOGGER.info(f"{'='*40}\n")

    def kill_session(self):
        """Kill the entire tmux session and all its processes."""
        try:
            # First, try to send Ctrl+C to all windows to gracefully stop pipelines
            result = subprocess.run(
                ["tmux", "list-windows", "-t", self.session_name, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                windows = result.stdout.strip().split("\n")
                for window in windows:
                    if window and window != "control":  # Don't send to control window
                        # Send Ctrl+C to each pipeline window
                        subprocess.run(
                            ["tmux", "send-keys", "-t", f"{self.session_name}:{window}", "C-c"],
                            capture_output=True,
                        )

                # Give processes time to cleanup gracefully
                time.sleep(1)
        except Exception as e:
            LOGGER.debug(f"Could not send termination signals to tmux windows: {e}")

        result = subprocess.run(
            ["tmux", "kill-session", "-t", self.session_name],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            LOGGER.info(f"✓ Killed tmux session '{self.session_name}'")
        else:
            LOGGER.debug(f"Tmux session '{self.session_name}' was already gone")


class StreamManager:
    """Manages multiple concurrent GStreamer video streams using tmux."""

    _atexit_registered = False

    def __init__(self, config_loader: ConfigLoader, verbose: bool = False):
        self.config_loader = config_loader
        self.imu_senders: list[IMUSender] = []
        self.tmux_manager: TmuxSessionManager | None = None
        self._shutdown = threading.Event()
        self._stopped = False
        self._cleanup_lock = threading.Lock()
        self.verbose = verbose

        # Register atexit handler once per instance
        if not StreamManager._atexit_registered:
            atexit.register(lambda: self.stop_all() if not self._stopped else None)
            StreamManager._atexit_registered = True
            LOGGER.debug("Registered atexit cleanup handler")

    def add_stream(
        self, stream_config: StreamConfig, stream_type: str, encoder_preference: str, bitrate: int
    ):
        """Add a video stream to manager."""
        # Initialize tmux manager on first stream
        if self.tmux_manager is None:
            self.tmux_manager = TmuxSessionManager()

        use_h264_for_depth = False
        if stream_type == "depth":
            encoding_cfg = self.config_loader.config.get("encoding", {})
            depth_h264_cfg = encoding_cfg.get("depth_h264", {})
            use_h264_for_depth = depth_h264_cfg.get("use_h264", True)

        encoder = EncoderFactory.create_encoder(
            encoder_preference, stream_type, use_h264_for_depth=use_h264_for_depth
        )

        strategy = StreamStrategyFactory.create_strategy(stream_type, self.config_loader)

        pipeline = strategy.build_sender_pipeline(
            device=stream_config.device,
            width=stream_config.width,
            height=stream_config.height,
            fps=stream_config.fps,
            fourcc=stream_config.fourcc,
            encoder=encoder,
            host=self.config_loader.get("network.server_ip"),
            port=stream_config.port,
            bitrate=bitrate,
        )

        self._run_pipeline_in_tmux(stream_config, pipeline, stream_type)

    def _run_pipeline_in_tmux(self, config: StreamConfig, pipeline_str: str, stream_type: str):
        """Run GStreamer pipeline in a tmux window."""
        label = format_stream_label(stream_type, None)
        window_name = f"{stream_type}_{config.port}"

        LOGGER.info(f"[{config.device}] Starting {label} stream on port {config.port}")
        if config.verbose:
            LOGGER.info(f"  Pipeline: {pipeline_str}")
        LOGGER.info(f"  Format: {config.fourcc}")
        LOGGER.info(f"  Resolution: {config.width}x{config.height}@{config.fps}fps")
        LOGGER.info(f"  Encoding: {config.encoding}")

        self.tmux_manager.create_window(window_name, pipeline_str)

    def add_imu_sender(self, serial: str, host: str, port: int):
        """Add IMU sender for camera."""
        try:
            imu_sender = IMUSender(serial, host, port)
            imu_sender.start()
            self.imu_senders.append(imu_sender)
        except Exception as e:
            LOGGER.warning(f"Failed to start IMU for {serial}: {e}")

    def wait(self):
        """Block until shutdown is requested (Ctrl+C or signal)."""
        try:
            if self.tmux_manager:
                if self.verbose:
                    self.tmux_manager.attach()
                else:
                    LOGGER.info(f"Streams are running in {self.session_name} tmux session.")
            LOGGER.info("All streams running. Press Ctrl+C to stop.")

            # Use timeout loop to allow KeyboardInterrupt to be caught
            while not self._shutdown.is_set():
                self._shutdown.wait(timeout=0.5)

        except KeyboardInterrupt:
            LOGGER.info("\nReceived KeyboardInterrupt. Shutting down...")
        finally:
            self.stop_all()

    def stop_all(self):
        """Stop all running streams, IMU senders, and tmux session (idempotent)."""
        with self._cleanup_lock:
            if self._stopped:
                LOGGER.debug("Cleanup already performed, skipping")
                return

            self._stopped = True
            self._shutdown.set()

            LOGGER.info("\n" + "=" * 40)
            LOGGER.info("SHUTTING DOWN - Stopping all streams...")
            LOGGER.info("=" * 40)

            # Stop IMU senders first
            if self.imu_senders:
                LOGGER.info("Stopping IMU senders...")
                for imu in self.imu_senders:
                    try:
                        imu.stop()
                        LOGGER.debug(f"  ✓ Stopped IMU for {imu.serial}")
                    except Exception as e:
                        LOGGER.warning(f"  Error stopping IMU for {imu.serial}: {e}")

            # Kill tmux session and all pipelines
            if self.tmux_manager:
                try:
                    LOGGER.info("Stopping GStreamer pipelines...")
                    self.tmux_manager.kill_session()
                except Exception as e:
                    LOGGER.warning(f"Error during tmux cleanup: {e}")

            LOGGER.info("=" * 70)
            LOGGER.info("✓ All streams stopped. Clean exit.")
            LOGGER.info("=" * 70 + "\n")


def find_best_mode(
    device: DeviceInfo,
    target_size: tuple[int, int],
    stream_type: str,
    target_format: str | None = None,
) -> Mode | None:
    """Find best matching mode for requested size and stream type."""
    key = (stream_type or "").strip().lower()

    if key == "depth":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_DEPTH]
    elif key == "color":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_COLOR]
    elif key == "infra_stereo":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_IR_STEREO]
    elif key == "infra":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_IR]
    else:
        return None

    if target_format and target_format.lower() != "auto":
        filtered_candidates = [
            m for m in candidates if m.fourcc.strip().upper() == target_format.strip().upper()
        ]
        if not filtered_candidates:
            LOGGER.warning(
                f"Format '{target_format}' not found for {stream_type} on {device.dev}. Ignoring format constraint."
            )
        else:
            candidates = filtered_candidates

    if not candidates:
        return None

    w, h = target_size

    if key == "infra_stereo":
        target_y8i_width = w * 2
        target_pixels = target_y8i_width * h

        for mode in candidates:
            if mode.size == (target_y8i_width, h):
                return mode

        return min(candidates, key=lambda m: abs(m.size[0] * m.size[1] - target_pixels))
    else:
        target_pixels = w * h

        for mode in candidates:
            if mode.size == target_size:
                return mode

        return min(candidates, key=lambda m: abs(m.size[0] * m.size[1] - target_pixels))


def get_best_fps(mode: Mode, requested: int | None = None) -> int:
    """Get best FPS for mode."""
    if requested is None:
        return max(mode.fps_list)

    valid = [f for f in mode.fps_list if f <= requested]
    if valid:
        return max(valid)

    return min(mode.fps_list, key=lambda f: abs(f - requested))


def install_signal_handlers(manager: "StreamManager"):
    """Install signal handlers for graceful shutdown."""
    signal_count = {"count": 0, "last_time": 0.0}

    def _handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        current_time = time.time()

        # Check for double Ctrl+C (within 2 seconds)
        if current_time - signal_count["last_time"] < 2:
            signal_count["count"] += 1
            if signal_count["count"] >= 2:
                LOGGER.warning("\nForce quit detected! Terminating immediately...")
                os._exit(1)  # Force immediate exit
        else:
            signal_count["count"] = 1

        signal_count["last_time"] = current_time

        LOGGER.info(f"\nReceived {sig_name} signal. Initiating shutdown...")
        LOGGER.info("(Press Ctrl+C again within 2 seconds to force quit)")
        manager._shutdown.set()

    # Install handlers for common termination signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old_handler = signal.signal(sig, _handle_signal)
            LOGGER.debug(f"Installed signal handler for {signal.Signals(sig).name}")
        except (OSError, RuntimeError, ValueError) as e:
            LOGGER.warning(f"Could not install handler for signal {sig}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="RealSense Multi-Stream Sender with tmux Integration"
    )
    parser.add_argument("--config", default="src/config/config.yaml", help="Configuration file")
    parser.add_argument("--host", help="Override server IP from config")
    parser.add_argument("--resolution", help="Override resolution (WIDTHxHEIGHT)")
    parser.add_argument("--fps", type=int, help="Override target FPS")
    parser.add_argument(
        "--encoder", choices=["auto", "nvh264enc", "x264enc"], help="Override encoder preference"
    )
    parser.add_argument("--bitrate", type=int, help="Override H.264 bitrate (kbps)")
    parser.add_argument("--no-imu", action="store_true", help="Disable IMU streaming")
    parser.add_argument("--list-only", action="store_true", help="List cameras and exit")
    parser.add_argument("--device", help="Stream only specific device")
    parser.add_argument(
        "--preset",
        choices=["d435i", "d455", "d415", "l515"],
        help="Use camera preset",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    # Load configuration
    config_loader = ConfigLoader(args.config)

    # Get configurations with overrides
    network_cfg = config_loader.get_network_config(args)
    camera_cfg = config_loader.get_camera_config(args)
    encoding_cfg = config_loader.get_encoding_config(args)
    imu_cfg = config_loader.get_imu_config(args)

    # Validate network config
    try:
        validate_network_config(network_cfg)
    except ValueError as e:
        LOGGER.error(f"Configuration error: {e}")
        sys.exit(1)

    # Parse resolution
    try:
        target_size = parse_resolution(camera_cfg["resolution"])
    except ValueError as e:
        LOGGER.error(f"Resolution error: {e}")
        sys.exit(1)

    # Detect cameras
    LOGGER.info("Detecting RealSense cameras...")
    cameras = RealSenseDetector.detect_all_cameras()

    if not cameras:
        LOGGER.error("✗ No RealSense cameras detected!")
        sys.exit(1)

    if args.device:
        cameras = [c for c in cameras if c.dev == args.device]
        if not cameras:
            LOGGER.error(f"✗ Device {args.device} not found!")
            sys.exit(1)

    LOGGER.info(f"✓ Found {len(cameras)} RealSense camera(s)\n")

    # Display camera info
    for i, cam in enumerate(cameras, 1):
        LOGGER.info(f"Camera {i}: {cam.model} ({cam.dev})")
        if cam.serial:
            LOGGER.info(f"  Serial: {cam.serial}")

    # Auto-detect preset if not provided
    if not args.preset and cameras:
        detected_model = cameras[0].model.lower()
        if "435i" in detected_model or "d435i" in detected_model:
            args.preset = "d435i"
            LOGGER.info(f"\n✓ Auto-detected D435i camera, using d435i preset\n")
        elif "455" in detected_model or "d455" in detected_model:
            args.preset = "d455"
            LOGGER.info(f"\n✓ Auto-detected D455 camera, using d455 preset\n")
        elif "415" in detected_model or "d415" in detected_model:
            args.preset = "d415"
            LOGGER.info(f"\n✓ Auto-detected D415 camera, using d415 preset\n")
        elif "l515" in detected_model:
            args.preset = "l515"
            LOGGER.info(f"\n✓ Auto-detected L515 camera, using l515 preset\n")

    if args.list_only:
        sys.exit(0)

    # Create manager and install signal handlers
    manager = StreamManager(config_loader, verbose=args.verbose)
    install_signal_handlers(manager)

    camera_groups = RealSenseDetector.group_by_serial(cameras)

    # Start IMU senders first
    for serial, serial_cameras in camera_groups.items():
        if imu_cfg["enabled"] and serial != "unknown":
            LOGGER.info(f"\nStarting IMU for camera {serial}")
            manager.add_imu_sender(
                serial,
                network_cfg["server_ip"],
                network_cfg["imu_port"],
            )

    use_h264_for_depth = encoding_cfg.get("depth_h264", {}).get("use_h264", True)
    depth_bitrate = encoding_cfg.get("depth_h264", {}).get("bitrate", 8000)
    h264_bitrate = encoding_cfg.get("h264", {}).get("bitrate", 4000)

    # Use preset configuration if available
    if args.preset:
        preset = config_loader.get_preset(args.preset)
        if preset:
            LOGGER.info(f"\nUsing {args.preset.upper()} preset configuration:")

            camera_by_type = {}
            for cam in cameras:
                stream_type = get_stream_type_from_fourcc(cam.modes[0].fourcc)
                camera_by_type[stream_type] = cam

            for stream_def in preset["streams"]:
                stream_name = stream_def["name"]
                encoding = stream_def["encoding"]
                port_offset = stream_def["port_offset"]

                cam = None
                if stream_name == "depth":
                    cam = camera_by_type.get("depth")
                elif stream_name == "color":
                    cam = camera_by_type.get("color")
                elif stream_name == "infra_stereo":
                    for c in cameras:
                        if c.modes and c.modes[0].fourcc.strip().upper() == "Y8I":
                            cam = c
                            break
                    if not cam:
                        cam = camera_by_type.get("infra")
                elif stream_name == "infra1":
                    cam = camera_by_type.get("infra")

                if not cam:
                    LOGGER.info(f"  No camera found for {stream_name}, skipping")
                    continue

                if stream_name == "infra_stereo":
                    target_format = camera_cfg.get("infra_format", "Y8I")
                    mode_target_size = (target_size[0] * 2, target_size[1])
                    actual_stream_type = "infra_stereo"
                elif stream_name == "depth":
                    target_format = camera_cfg.get("depth_format")
                    mode_target_size = target_size
                    actual_stream_type = "depth"
                elif stream_name == "color":
                    target_format = camera_cfg.get("color_format")
                    mode_target_size = target_size
                    actual_stream_type = "color"
                else:
                    target_format = None
                    mode_target_size = target_size
                    actual_stream_type = (
                        stream_name.replace("infra", "infra").replace("1", "").replace("2", "")
                    )

                mode = find_best_mode(cam, mode_target_size, actual_stream_type, target_format)

                if not mode:
                    LOGGER.info(f"  No suitable mode for {stream_name} on {cam.dev}")
                    continue

                fps = get_best_fps(mode, camera_cfg["fps"])
                port = network_cfg["base_port"] + port_offset

                stream_cfg = StreamConfig(
                    name=stream_name,
                    port=port,
                    encoding=encoding,
                    width=mode.size[0],
                    height=mode.size[1],
                    fps=fps,
                    device=cam.dev,
                    fourcc=mode.fourcc,
                    verbose=args.verbose,
                )

                if stream_name == "depth":
                    actual_bitrate = depth_bitrate
                else:
                    actual_bitrate = h264_bitrate

                manager.add_stream(
                    stream_cfg, actual_stream_type, encoding_cfg["encoder"], actual_bitrate
                )

    time.sleep(1)
    LOGGER.info(f"{'='*40}")
    LOGGER.info("ALL STREAMS STARTED")
    LOGGER.info(f"{'='*40}")
    LOGGER.info("Press Ctrl+C to stop all streams")

    try:
        manager.wait()
    except Exception as e:
        LOGGER.error(f"Unexpected error in main loop: {e}")
        manager.stop_all()


if __name__ == "__main__":
    main()
