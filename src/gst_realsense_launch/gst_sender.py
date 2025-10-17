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

try:
    import pyrealsense2 as rs
except ImportError:
    rs = None
    print("Warning: pyrealsense2 not available. Hardware IMU streaming disabled.")


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
            print(f"Warning: Could not query RealSense devices via SDK: {e}")

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
                pass

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

                        print(
                            f"Warning: Multiple RealSense devices detected. Using first device serial."
                        )
                        return devices[0].get_info(rs.camera_info.serial_number)

                except Exception as e:
                    print(f"Warning: Could not get USB path for {dev}: {e}")

            except Exception as e:
                print(f"Warning: Could not query RealSense SDK for serial: {e}")

        try:
            result = cls.run_cmd(["udevadm", "info", "--query=property", f"--name={dev}"])
            for line in result.stdout.splitlines():
                if "ID_SERIAL_SHORT=" in line:
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass

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
            print(f"Warning: Failed to probe {dev}: {e}", file=sys.stderr)
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
        print(f"✓ IMU streaming started for {self.serial}")

    def _stream_loop(self):
        pipeline_started = False
        try:
            ctx = rs.context()
            devices = ctx.query_devices()

            device_found = False
            for dev in devices:
                if dev.get_info(rs.camera_info.serial_number) == self.serial:
                    device_found = True
                    print(f"  Found IMU device: {dev.get_info(rs.camera_info.name)}")
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
                    raise

        except Exception as e:
            print(f"IMU error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            if pipeline_started:
                try:
                    self.pipeline.stop()
                except Exception:
                    pass
            self.sock.close()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.sock.close()


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
        """Create tmux session if it doesn't exist."""
        # Check if session already exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True
        )

        if result.returncode != 0:
            # Create new detached session
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name, "-n", "control"], check=True
            )
            print(f"✓ Created tmux session '{self.session_name}'")
        else:
            print(f"✓ Using existing tmux session '{self.session_name}'")

    def create_window(self, window_name: str, command: str):
        """Create a new tmux window and run command in it.

        Args:
            window_name: Name for the tmux window
            command: Command to execute in the window
        """
        self.window_count += 1

        # Create new window
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

        # Send command to the window
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{self.session_name}:{window_name}",
                command,
                "C-m",  # Enter key
            ],
            check=True,
        )

        print(f"  ✓ Created window '{window_name}' in tmux")

    def attach(self):
        """Attach to the tmux session (for interactive use)."""
        print(f"\nTo view the streams, attach to tmux session:")
        print(f"  tmux attach -t {self.session_name}")
        print(f"\nTmux navigation:")
        print(f"  Ctrl+b n : next window")
        print(f"  Ctrl+b p : previous window")
        print(f"  Ctrl+b [0-9] : select window by number")
        print(f"  Ctrl+b d : detach from session")
        print(f"  Ctrl+b & : kill current window")

    def kill_session(self):
        """Kill the entire tmux session."""
        subprocess.run(["tmux", "kill-session", "-t", self.session_name], capture_output=True)
        print(f"✓ Killed tmux session '{self.session_name}'")


class StreamManager:
    """Manages multiple concurrent GStreamer video streams using tmux."""

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.imu_senders: list[IMUSender] = []
        self.tmux_manager = TmuxSessionManager()
        self._shutdown = threading.Event()
        self._stopped = False

    def add_stream(
        self, stream_config: StreamConfig, stream_type: str, encoder_preference: str, bitrate: int
    ):
        """Add a video stream to manager."""
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

        # Run pipeline in tmux
        self._run_pipeline_in_tmux(stream_config, pipeline, stream_type)

    def _run_pipeline_in_tmux(self, config: StreamConfig, pipeline_str: str, stream_type: str):
        """Run GStreamer pipeline in a tmux window."""
        label = format_stream_label(stream_type, None)

        # Create a descriptive window name
        window_name = f"{stream_type}_{config.port}"

        print(f"\n[{config.device}] Starting {label} stream on port {config.port}")
        print(f"  Pipeline: {pipeline_str}")
        print(f"  Format: {config.fourcc}")
        print(f"  Resolution: {config.width}x{config.height}@{config.fps}fps")
        print(f"  Encoding: {config.encoding}")

        # Create the window and run the pipeline
        self.tmux_manager.create_window(window_name, pipeline_str)

    def add_imu_sender(self, serial: str, host: str, port: int):
        """Add IMU sender for camera."""
        try:
            imu_sender = IMUSender(serial, host, port)
            imu_sender.start()
            self.imu_senders.append(imu_sender)
        except Exception as e:
            print(f"Warning: Failed to start IMU for {serial}: {e}")

    def wait(self):
        """Block until shutdown is requested (Ctrl+C or signal)."""
        try:
            self.tmux_manager.attach()
            print("\nAll streams running. Press Ctrl+C to stop.\n")
            self._shutdown.wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_all()

    def stop_all(self):
        """Stop all running streams, IMU senders, and tmux session (idempotent)."""
        self._stopped = True
        self._shutdown.set()

        print("\n\nStopping all streams...")
        try:
            self.tmux_manager.kill_session()
        except Exception as e:
            print(f"tmux cleanup warning: {e}")

        for imu in self.imu_senders:
            try:
                imu.stop()
            except Exception:
                pass

        print("✓ Clean exit.")


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
            print(
                f"⚠️  Warning: Format '{target_format}' not found for {stream_type} on {device.dev}. Ignoring format constraint."
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
    import os

    handled = {"fired": False}

    def _handle(signum, _frame):
        if handled["fired"]:
            return
        handled["fired"] = True

        try:
            manager._shutdown.set()
        except Exception:
            pass
        try:
            os.write(2, f"\nReceived signal {signum}. Shutting down...\n".encode())
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handle)
            try:
                signal.siginterrupt(sig, False)
            except Exception:
                pass
        except Exception:
            pass


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
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse resolution
    try:
        target_size = parse_resolution(camera_cfg["resolution"])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Detect cameras
    print("Detecting RealSense cameras...")
    cameras = RealSenseDetector.detect_all_cameras()

    if not cameras:
        print("✗ No RealSense cameras detected!", file=sys.stderr)
        sys.exit(1)

    if args.device:
        cameras = [c for c in cameras if c.dev == args.device]
        if not cameras:
            print(f"✗ Device {args.device} not found!", file=sys.stderr)
            sys.exit(1)

    print(f"✓ Found {len(cameras)} RealSense camera(s)\n")

    # Display camera info
    for i, cam in enumerate(cameras, 1):
        print(f"Camera {i}: {cam.model} ({cam.dev})")
        if cam.serial:
            print(f"  Serial: {cam.serial}")

    # Auto-detect preset if not provided
    if not args.preset and cameras:
        detected_model = cameras[0].model.lower()
        if "435i" in detected_model or "d435i" in detected_model:
            args.preset = "d435i"
            print(f"\n✓ Auto-detected D435i camera, using d435i preset\n")
        elif "455" in detected_model or "d455" in detected_model:
            args.preset = "d455"
            print(f"\n✓ Auto-detected D455 camera, using d455 preset\n")
        elif "415" in detected_model or "d415" in detected_model:
            args.preset = "d415"
            print(f"\n✓ Auto-detected D415 camera, using d415 preset\n")
        elif "l515" in detected_model:
            args.preset = "l515"
            print(f"\n✓ Auto-detected L515 camera, using l515 preset\n")

    if args.list_only:
        sys.exit(0)

    manager = StreamManager(config_loader)

    install_signal_handlers(manager)
    atexit.register(manager.stop_all)

    camera_groups = RealSenseDetector.group_by_serial(cameras)

    # Start IMU senders first
    for serial, serial_cameras in camera_groups.items():
        if imu_cfg["enabled"] and serial != "unknown":
            print(f"\nStarting IMU for camera {serial}")
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
            print(f"\nUsing {args.preset.upper()} preset configuration:")

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
                    print(f" No camera found for {stream_name}, skipping")
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
                    print(f" No suitable mode for {stream_name} on {cam.dev}")
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
                )

                if stream_name == "depth":
                    actual_bitrate = depth_bitrate
                else:
                    actual_bitrate = h264_bitrate

                manager.add_stream(
                    stream_cfg, actual_stream_type, encoding_cfg["encoder"], actual_bitrate
                )

    time.sleep(1)
    print(f"\n{'='*70}")
    print("ALL STREAMS STARTED")
    print(f"{'='*70}")
    manager.wait()


if __name__ == "__main__":
    main()
