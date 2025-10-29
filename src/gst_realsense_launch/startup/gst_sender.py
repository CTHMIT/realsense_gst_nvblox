#!/usr/bin/env python3
"""
RealSense Multi-Stream Sender with tmux Integration

Modified to run each GStreamer pipeline in a separate tmux window for better
visibility and debugging capabilities.
"""


import argparse
import atexit
import getpass
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from utils.logger import LOGGER

try:
    import pyrealsense2 as rs
except Exception:
    rs = None
    LOGGER.warning("pyrealsense2 not available; RealSense-dependent features disabled.")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gst_realsense_launch.startup.rs_common import (
    ConfigLoader,
    StreamConfig,
    format_stream_label,
    get_stream_type_from_fourcc,
    parse_resolution,
    validate_network_config,
)
from gst_realsense_launch.startup.rs_core import EncoderFactory, StreamStrategyFactory
from gst_realsense_launch.startup.rs_detect import RealSenseDetector
from gst_realsense_launch.startup.rs_tmux import TmuxSessionManager


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

        encoder = EncoderFactory.create_encoder(
            encoder_preference,
            stream_type,
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
                    self.tmux_manager.attach_info()
                else:
                    LOGGER.info(
                        f"Streams are running in {self.tmux_manager.session_name} tmux session."
                    )
            LOGGER.info("All streams running. Press Ctrl+C to stop.")

            while not self._shutdown.is_set():
                self._shutdown.wait(timeout=0.5)

        except KeyboardInterrupt:
            LOGGER.info("Received KeyboardInterrupt. Shutting down...")
        finally:
            self.kill_gst_launch()
            self.stop_all()

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
                # 沒權限時保留在 remaining
                pass

        killed = [pid for pid in initial if pid not in remaining]
        return (killed, sorted(list(remaining)))

    def stop_all(self):
        """Stop all running streams, IMU senders, and tmux session (idempotent)."""
        with self._cleanup_lock:
            if self._stopped:
                LOGGER.debug("Cleanup already performed, skipping")
                return

            self._stopped = True
            self._shutdown.set()

            LOGGER.info("=" * 40)
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
            LOGGER.info("=" * 70)


def check_encoder_availability():
    """Check and report available encoders."""
    encoders = {"nvh264enc": "NVIDIA H.264 (hardware)", "x264enc": "x264 H.264 (software)"}

    available = []
    missing = []

    for encoder, description in encoders.items():
        result = subprocess.run(
            ["gst-inspect-1.0", encoder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            available.append(f"  ✓ {encoder}: {description}")
        else:
            missing.append(f"  ✗ {encoder}: {description}")

    if available:
        LOGGER.info("Available encoders:")
        for enc in available:
            LOGGER.info(enc)

    if missing:
        LOGGER.warning("Missing encoders:")
        for enc in missing:
            LOGGER.warning(enc)
        LOGGER.warning("To install missing encoders:")
        LOGGER.warning("  sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly")

    return len(available) > 0


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
                LOGGER.warning("Force quit detected! Terminating immediately...")
                os._exit(1)  # Force immediate exit
        else:
            signal_count["count"] = 1

        signal_count["last_time"] = current_time

        LOGGER.info(f"Received {sig_name} signal. Initiating shutdown...")
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
        "--encoder",
        choices=["auto", "nvh264enc", "x264enc"],
        help="Override encoder preference",
    )
    parser.add_argument("--bitrate", type=int, help="Override H.264 bitrate (kbps)")
    parser.add_argument("--no-imu", action="store_true", help="Disable IMU streaming")
    parser.add_argument("--list-only", action="store_true", help="List cameras and exit")
    parser.add_argument("--device", help="Stream only specific device")
    parser.add_argument(
        "--preset",
        choices=["d435i"],
        help="Use camera preset",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    check_encoder_availability()

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
    realsense_detector = RealSenseDetector()
    cameras = RealSenseDetector.detect_all_cameras()

    if not cameras:
        LOGGER.error("✗ No RealSense cameras detected!")
        sys.exit(1)

    if args.device:
        cameras = [c for c in cameras if c.dev == args.device]
        if not cameras:
            LOGGER.error(f"✗ Device {args.device} not found!")
            sys.exit(1)

    LOGGER.info(f"✓ Found {len(cameras)} RealSense camera(s)")

    for i, cam in enumerate(cameras, 1):
        LOGGER.info(f"Camera {i}: {cam.model} ({cam.dev})")
        if cam.serial:
            LOGGER.info(f"  Serial: {cam.serial}")

    if not args.preset and cameras:
        detected_model = cameras[0].model.lower()
        if "435i" in detected_model or "d435i" in detected_model:
            args.preset = "d435i"
            LOGGER.info(f"✓ Auto-detected D435i camera, using d435i preset")

    if args.list_only:
        sys.exit(0)

    manager = StreamManager(config_loader, verbose=args.verbose)
    install_signal_handlers(manager)

    camera_groups = RealSenseDetector.group_by_serial(cameras)

    for serial, serial_cameras in camera_groups.items():
        if imu_cfg["enabled"] and serial != "unknown":
            if args.verbose:
                LOGGER.info(f"Starting IMU for camera {serial}")
            manager.add_imu_sender(
                serial,
                network_cfg["server_ip"],
                network_cfg["imu_port"],
            )

    actual_bitrate = encoding_cfg.get("h264", {}).get("bitrate", 4000)

    if args.preset:
        preset = config_loader.get_preset(args.preset)

        if preset:
            if args.verbose:
                LOGGER.info(f"Using {args.preset.upper()} preset configuration:")

            camera_by_type = {}
            for cam in cameras:
                stream_type = get_stream_type_from_fourcc(cam.modes[0].fourcc)
                camera_by_type[stream_type] = cam

            for stream_def in preset["streams"]:
                stream_name = stream_def["name"]
                encoding = stream_def["encoding"]
                port = stream_def["port"]

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

                mode = realsense_detector.find_best_mode(
                    cam, mode_target_size, actual_stream_type, target_format
                )

                if not mode:
                    LOGGER.info(f"  No suitable mode for {stream_name} on {cam.dev}")
                    continue

                fps = realsense_detector.get_best_fps(mode, camera_cfg["fps"])

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

                manager.add_stream(
                    stream_cfg, actual_stream_type, encoding_cfg["encoder"], actual_bitrate
                )

        else:
            LOGGER.error(f"Preset '{args.preset}' not found in configuration")
            sys.exit(1)
    else:
        LOGGER.error("No preset specified. Use --preset option")
        sys.exit(1)

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
