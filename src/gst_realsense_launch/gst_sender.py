#!/usr/bin/env python3
"""
RealSense Multi-Stream Sender with Strategy Pattern

Automatically detects RealSense cameras and streams all available feeds
(depth, color, IR left/right, IMU) over network using GStreamer.

Architecture:
- Uses Strategy Pattern for encoding and stream handling
- YAML-based configuration with command-line overrides
- Supports multiple camera types (D435i, D455, D415, L515)
"""

import argparse
import json
import os
import re
import shlex
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


from .rs_common import (
    FOURCC_COLOR,
    FOURCC_DEPTH,
    FOURCC_IR,
    ConfigLoader,
    StreamConfig,
    format_stream_label,
    get_stream_type_from_fourcc,
    parse_resolution,
    validate_network_config,
)
from .rs_core import EncoderFactory, StreamStrategyFactory


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

    This class scans /dev/videoX devices and identifies RealSense cameras
    by analyzing their capabilities and metadata.
    """

    # RealSense model detection patterns
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
    def extract_model(cls, card_name: str, dev: str | None = None) -> str:
        """Extract RealSense model from card name or device properties."""
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
    def extract_serial(cls, dev: str) -> str | None:
        """Extract serial number from device using udev."""
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
        """
        Probe V4L2 device and extract all capabilities.

        Returns DeviceInfo if device is a RealSense camera, None otherwise.
        """
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
            serial = cls.extract_serial(dev)

            # Parse formats
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
        """Parse v4l2-ctl format output into Mode objects."""
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
                current_fourcc = m_fmt.group(1)
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
        """Detect all RealSense cameras on system."""
        cameras = []
        for dev in cls.list_video_nodes():
            info = cls.probe_device(dev)
            if info:
                cameras.append(info)
        return cameras

    @staticmethod
    def group_by_serial(cameras: list[DeviceInfo]) -> dict:
        """Group cameras by serial number to identify multi-sensor devices."""
        grouped: dict = {}
        for cam in cameras:
            serial = cam.serial or "unknown"
            if serial not in grouped:
                grouped[serial] = []
            grouped[serial].append(cam)
        return grouped


class IMUSender:
    """
    Sends IMU data from RealSense camera over UDP.

    Captures accelerometer and gyroscope data from RealSense hardware
    and transmits as JSON packets over network.
    """

    def __init__(self, serial: str, host: str, port: int):
        """
        Initialize IMU sender.

        Args:
            serial: Camera serial number
            host: Target IP address
            port: Target UDP port
        """
        if not rs:
            raise RuntimeError("pyrealsense2 not available")

        self.serial = serial
        self.host = host
        self.port = port
        self.running: bool = False
        self.thread: threading.Thread | None = None

        # Initialize RealSense pipeline
        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_device(serial)
        self.config.enable_stream(rs.stream.accel)
        self.config.enable_stream(rs.stream.gyro)

        # UDP socket for sending
        import socket

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self):
        """Start IMU data streaming in background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._stream_loop, daemon=True)
        self.thread.start()
        print(f"✓ IMU streaming started for {self.serial}")

    def _stream_loop(self):
        """Main loop for streaming IMU data."""
        pipeline_started = False  # Track if pipeline actually started
        try:
            self.pipeline.start(self.config)
            pipeline_started = True  # Only set to True after successful start

            while self.running:
                frames = self.pipeline.wait_for_frames()

                accel_frame = frames.first_or_default(rs.stream.accel)
                gyro_frame = frames.first_or_default(rs.stream.gyro)

                if accel_frame and gyro_frame:
                    accel = accel_frame.as_motion_frame().get_motion_data()
                    gyro = gyro_frame.as_motion_frame().get_motion_data()

                    # Create JSON packet
                    imu_data = {
                        "type": "imu",
                        "timestamp": time.time(),
                        "serial": self.serial,
                        "accel": {
                            "x": accel.x,
                            "y": accel.y,
                            "z": accel.z,
                        },
                        "gyro": {
                            "x": gyro.x,
                            "y": gyro.y,
                            "z": gyro.z,
                        },
                    }

                    # Send over UDP
                    data = json.dumps(imu_data).encode("utf-8")
                    self.sock.sendto(data, (self.host, self.port))

        except Exception as e:
            print(f"IMU streaming error: {e}")
        finally:
            # Only stop if pipeline was successfully started
            if pipeline_started:
                try:
                    self.pipeline.stop()
                except Exception as e:
                    print(f"Warning: Error stopping IMU pipeline: {e}")
            self.sock.close()

    def stop(self):
        """Stop IMU streaming."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.sock.close()


class StreamManager:
    """
    Manages multiple concurrent GStreamer video streams.

    Handles starting, monitoring, and stopping multiple video pipelines
    using the Strategy Pattern for different stream types.
    """

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.processes: list[tuple[StreamConfig, subprocess.Popen]] = []
        self.threads: list[threading.Thread] = []
        self.imu_senders: list[IMUSender] = []

    def add_stream(
        self, stream_config: StreamConfig, stream_type: str, encoder_preference: str, bitrate: int
    ):
        """
        Add a video stream to manager.

        Args:
            stream_config: Stream configuration
            stream_type: Stream type (depth, color, ir)
            encoder_preference: Encoder preference
            bitrate: Target bitrate for H.264
        """
        # Create encoder and stream strategy
        encoder = EncoderFactory.create_encoder(encoder_preference, stream_type)
        strategy = StreamStrategyFactory.create_strategy(stream_type)

        # Build GStreamer pipeline
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

        # Start stream in thread
        thread = threading.Thread(
            target=self._run_pipeline, args=(stream_config, pipeline, stream_type), daemon=True
        )
        self.threads.append(thread)
        thread.start()

    def _run_pipeline(self, config: StreamConfig, pipeline_str: str, stream_type: str):
        """Run GStreamer pipeline and monitor output."""
        label = format_stream_label(stream_type, None)

        print(f"\n[{config.device}] Starting {label} stream on port {config.port}")
        print(f"  Resolution: {config.width}x{config.height}@{config.fps}fps")
        print(f"  Encoding: {config.encoding}")

        # Securely handle pipelines that use shell operators like '|'
        if "|" in pipeline_str:
            # Split pipeline into commands
            v4l2_cmd_str, gst_cmd_str = pipeline_str.split("|", 1)
            v4l2_cmd = shlex.split(v4l2_cmd_str)
            gst_cmd = shlex.split(gst_cmd_str)

            # Start v4l2-ctl process
            v4l2_proc = subprocess.Popen(v4l2_cmd, stdout=subprocess.PIPE)

            # Start gst-launch-1.0 process, taking input from v4l2-ctl
            proc = subprocess.Popen(
                gst_cmd,
                stdin=v4l2_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        else:
            # For simple pipelines, no shell is needed
            cmd = shlex.split(pipeline_str)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        self.processes.append((config, proc))

        try:
            if proc.stdout:
                for line in iter(proc.stdout.readline, b""):
                    if line:
                        line_str = line.decode("utf-8", errors="ignore").strip()
                        if "ERROR" in line_str or "WARNING" in line_str:
                            print(f"  [{config.device}] {line_str}")
        except KeyboardInterrupt:
            pass

    def add_imu_sender(self, serial: str, host: str, port: int):
        """Add IMU sender for camera."""
        try:
            imu_sender = IMUSender(serial, host, port)
            imu_sender.start()
            self.imu_senders.append(imu_sender)
        except Exception as e:
            print(f"Warning: Failed to start IMU for {serial}: {e}")

    def wait(self):
        """Wait for all streams (Ctrl+C to stop)."""
        try:
            print("\nAll streams running. Press Ctrl+C to stop.\n")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop_all()

    def stop_all(self):
        """Stop all running streams and IMU senders."""
        print("\n\nStopping all streams...")

        # Stop video streams
        for config, proc in self.processes:
            try:
                proc.terminate()
                proc.wait(timeout=3)
                print(f"  ✓ Stopped {config.device}")
            except Exception:
                proc.kill()
                print(f"  ✗ Force killed {config.device}")

        # Stop IMU senders
        for imu in self.imu_senders:
            imu.stop()


def find_best_mode(
    device: DeviceInfo, target_size: tuple[int, int], stream_type: str
) -> Mode | None:
    """
    Find best matching mode for requested size and stream type.

    Tries exact match first, then finds closest resolution.
    """
    w, h = target_size
    target_pixels = w * h

    # Filter by stream type
    if stream_type == "depth":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_DEPTH]
    elif stream_type == "color":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_COLOR]
    elif stream_type == "ir":
        candidates = [m for m in device.modes if m.fourcc.strip().upper() in FOURCC_IR]
    else:
        candidates = device.modes

    if not candidates:
        return None

    # Exact match
    for mode in candidates:
        if mode.size == target_size:
            return mode

    # Closest match
    return min(candidates, key=lambda m: abs(m.size[0] * m.size[1] - target_pixels))


def get_best_fps(mode: Mode, requested: int | None = None) -> int:
    """Get best FPS for mode."""
    if requested is None:
        return max(mode.fps_list)

    valid = [f for f in mode.fps_list if f <= requested]
    if valid:
        return max(valid)

    return min(mode.fps_list, key=lambda f: abs(f - requested))


def main():
    parser = argparse.ArgumentParser(
        description="RealSense Multi-Stream Sender with Strategy Pattern"
    )
    parser.add_argument("--config", default="src/config/config.yaml", help="Configuration file")
    parser.add_argument(
        "--ports-config", default="src/config/ports.yaml", help="Ports allocation file"
    )
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

    args = parser.parse_args()

    # Load configuration
    config_loader = ConfigLoader(args.config, args.ports_config)

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

    if args.list_only:
        sys.exit(0)

    # Setup streams
    manager = StreamManager(config_loader)
    camera_groups = RealSenseDetector.group_by_serial(cameras)

    # Track which stream types we've seen to properly handle multiple IR sensors
    stream_type_counts: dict = {}

    for serial, serial_cameras in camera_groups.items():
        for cam in serial_cameras:
            stream_type = get_stream_type_from_fourcc(cam.modes[0].fourcc)

            mode = find_best_mode(cam, target_size, stream_type)
            if not mode:
                print(f"⚠  No suitable mode for {cam.dev}")
                continue

            fps = get_best_fps(mode, camera_cfg["fps"])

            # Handle multiple IR sensors by assigning them to infra1 and infra2
            stream_count = stream_type_counts.get(stream_type, 0)
            stream_type_counts[stream_type] = stream_count + 1

            # Map stream type to port key used in config.yaml
            if stream_type == "ir" and stream_count > 0:
                port_stream_type = f"infra{stream_count + 1}"
            else:
                port_stream_type = stream_type

            # Get the port directly from config.yaml's network.stream_ports section
            # No more base_port calculation - each stream has an explicit port
            port = config_loader.get_port_for_stream(port_stream_type, 5000)

            stream_cfg = StreamConfig(
                name=stream_type,
                port=port,
                encoding="jpeg2000" if stream_type == "depth" else "h264",
                width=mode.size[0],
                height=mode.size[1],
                fps=fps,
                device=cam.dev,
                fourcc=mode.fourcc,
            )

            manager.add_stream(
                stream_cfg, stream_type, encoding_cfg["encoder"], encoding_cfg["bitrate"]
            )

        # Start IMU streaming for this camera (one IMU per camera physical unit)
        if imu_cfg["enabled"] and serial != "unknown":
            manager.add_imu_sender(
                serial,
                network_cfg["server_ip"],
                network_cfg["imu_port"],  # Changed from metadata_port to imu_port
            )

    print(f"\n{'='*70}")
    print("ALL STREAMS STARTED")
    print(f"{'='*70}")
    manager.wait()


if __name__ == "__main__":
    main()
