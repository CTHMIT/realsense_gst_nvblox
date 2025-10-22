#!/usr/bin/env python3
"""RealSense Streaming Common Utilities and Configuration.

This module provides shared functionality for both sender and receiver:
- YAML configuration loading with command-line override support
- Common data structures
- Utility functions
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from utils.logger import LOGGER

try:
    import yaml
except ImportError:
    LOGGER.error("Error: pyyaml not found. Install: pip install pyyaml")
    sys.exit(1)


@dataclass
class StreamConfig:
    """Configuration for a single video stream."""

    name: str
    port: int
    encoding: str
    width: int
    height: int
    fps: int
    device: str | None = None
    fourcc: str | None = None
    verbose: bool = False


@dataclass
class CameraIntrinsics:
    """Camera intrinsic parameters for calibration."""

    width: int
    height: int
    fx: float
    fy: float
    ppx: float
    ppy: float
    distortion: list[float]

    def to_dict(self) -> dict:
        """Return a dictionary representation of the camera intrinsics."""
        return {
            "width": self.width,
            "height": self.height,
            "fx": self.fx,
            "fy": self.fy,
            "ppx": self.ppx,
            "ppy": self.ppy,
            "coeffs": self.distortion,
        }


class ConfigLoader:
    """Configuration loader with YAML file support and command-line override.

    This class implements a hierarchical configuration system:
    1. Default values from config.yaml
    2. Port allocations from ports.yaml (optional)
    3. Override with command-line arguments
    4. Validate and provide easy access to all settings
    """

    def __init__(self, config_file: str = "config/config.yaml") -> None:
        """Load configuration from YAML files.

        Args:
            config_file: Path to main YAML configuration file
        """
        self.config: dict = self._load_config_file(config_file)
        self.config_file = config_file

    def _load_config_file(self, config_file: str) -> dict:
        """Load YAML configuration file from multiple possible locations.

        Search order:
        1. Current directory
        2. Script directory
        3. User config directory (~/.config/realsense/)
        """
        search_paths = [
            Path(config_file),
            Path(__file__).parent / config_file,
            Path.home() / ".config" / "realsense" / config_file,
        ]

        for path in search_paths:
            if path.exists():
                try:
                    with path.open(encoding="utf-8") as f:
                        config = yaml.safe_load(f)
                        LOGGER.info(f"✓ Loaded configuration from {path}")
                        return cast(dict, config)
                except Exception as e:
                    LOGGER.warning(f"Warning: Failed to load {path}: {e}")
                    continue

        LOGGER.warning("Warning: No config file found. Using defaults.")
        return {}

    def get(self, key_path: str, default: Any = None, override: Any = None) -> Any:
        """Get configuration value with override support.

        Args:
            key_path: Dot-separated path to config value (e.g., "network.server_ip")
            default: Fallback value if not found
            override: Command-line override value (takes precedence)

        Returns:
            Configuration value (override > config file > default)
        """
        # If override is explicitly provided, use it
        if override is not None:
            return override

        # Try to get from config file
        keys = key_path.split(".")
        value: Any = self.config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value if value is not None else default

    def get_preset(self, preset_name: str) -> dict | None:
        """Get preset configuration for camera model."""
        presets = self.config.get("presets", {})
        return cast(dict | None, presets.get(preset_name))

    def get_port_for_stream(self, stream_type: str, fallback_port: int) -> int:
        """Get the designated port for a specific stream type.

        Args:
            stream_type: Type of stream (depth, color, infra1, infra2, imu, etc.)
            fallback_port: Port to use if no mapping exists

        Returns:
            The assigned port number for this stream type
        """
        stream_ports = self.get("network.stream_ports")

        if not stream_ports:
            return fallback_port

        stream_key = stream_type.lower()

        if stream_key in stream_ports:
            stream_config = stream_ports[stream_key]

            # IMU uses simple UDP
            if stream_key == "imu":
                return cast(int, stream_config.get("udp", fallback_port))

            # Video streams use RTP
            return cast(int, stream_config.get("rtp", fallback_port))

        return fallback_port

    def get_imu_port(self) -> int:
        """Get IMU UDP port from config.yaml.

        The IMU uses simple UDP packets (not RTP) for transmitting
        accelerometer and gyroscope data as JSON.

        Returns:
            Port number for IMU data transmission
        """
        # Try to get from network.stream_ports.imu.udp first
        stream_ports = self.get("network.stream_ports")

        if stream_ports and "imu" in stream_ports:
            imu_config = stream_ports["imu"]
            if isinstance(imu_config, dict) and "udp" in imu_config:
                return cast(int, imu_config["udp"])

        # Fallback to direct network.imu_port if it exists
        imu_port = self.get("network.imu_port")
        if imu_port is not None:
            return cast(int, imu_port)

        # Final fallback to default
        LOGGER.warning("Warning: IMU port not configured, using default port 5050")
        return 5050

    def get_network_config(self, args) -> dict:
        """Get complete network configuration with overrides."""
        # Handle both --imu-port and --metadata-port for backwards compatibility
        imu_port_override = getattr(args, "imu_port", None) or getattr(args, "metadata_port", None)

        return {
            "server_ip": self.get("network.server_ip", None, getattr(args, "host", None)),
            "base_port": self.get("network.base_port", 5000, getattr(args, "base_port", None)),
            "imu_port": self.get("network.stream_ports.imu.udp", 5050, imu_port_override),
            "info_url": self.get("network.info_url", None, getattr(args, "info_url", None)),
        }

    def get_camera_config(self, args) -> dict:
        """Get complete camera configuration with overrides."""
        return {
            "resolution": self.get(
                "camera.resolution", "640x480", getattr(args, "resolution", None)
            ),
            "fps": self.get("camera.fps", None, getattr(args, "fps", None)),
            "camera_name": self.get("camera.name", "camera", getattr(args, "camera_name", None)),
            "stream_preference": self.get(
                "camera.stream_preference", "auto", getattr(args, "stream", None)
            ),
        }

    def get_encoding_config(self, args) -> dict:
        """Get complete encoding configuration with overrides."""
        # Get H.264 configuration
        h264_config = {
            "bitrate": self.get("encoding.h264.bitrate", 4000, getattr(args, "bitrate", None)),
            "tune": self.get("encoding.h264.tune", "zerolatency"),
            "speed_preset": self.get("encoding.h264.speed_preset", "ultrafast"),
            "key_int_max": self.get("encoding.h264.key_int_max", 30),
        }

        # Get depth H.264 configuration
        depth_h264_config = {
            "bitrate": self.get("encoding.depth_h264.bitrate", 8000),
            "use_h264": self.get("encoding.depth_h264.use_h264", True),
        }

        # Get JPEG2000 configuration
        jpeg2000_config = {
            "quality": self.get("encoding.jpeg2000.quality", 100),
            "num_threads": self.get("encoding.jpeg2000.num_threads", 8),
        }

        return {
            "encoder": self.get("encoding.encoder", "auto", getattr(args, "encoder", None)),
            "h264": h264_config,
            "depth_h264": depth_h264_config,
            "jpeg2000": jpeg2000_config,
        }

    def get_imu_config(self, args) -> dict:
        """Get IMU configuration with overrides."""
        return {
            "enabled": self.get("imu.enabled", True, not getattr(args, "no_imu", False)),
            "publish_rate": self.get("imu.publish_rate", 200.0),
        }

    def get_receiver_config(self, args) -> dict:
        """Get receiver configuration with overrides."""
        return {
            "show_views": self.get(
                "receiver.show_views", False, getattr(args, "show_views", False)
            ),
            "view_scale": self.get("receiver.view_scale", 0.5, getattr(args, "view_scale", None)),
            "publish_odom": self.get(
                "receiver.publish_odom", True, getattr(args, "publish_odom", None)
            ),
            "odom_frame": self.get("receiver.odom_frame", "odom"),
            "base_link_frame": self.get("receiver.base_link_frame", "base_link"),
            "local_ip": self.get("network.local_ip", "0.0.0.0", getattr(args, "local_ip", None)),
        }

    def create_default_intrinsics(self, width: int, height: int) -> CameraIntrinsics:
        """Create default camera intrinsics based on image size.

        Uses ratios from config file to estimate focal length and principal point.
        """
        fx_ratio = self.get("calibration.default_intrinsics.fx_ratio", 0.6)
        fy_ratio = self.get("calibration.default_intrinsics.fy_ratio", 0.6)
        ppx_ratio = self.get("calibration.default_intrinsics.ppx_ratio", 0.5)
        ppy_ratio = self.get("calibration.default_intrinsics.ppy_ratio", 0.5)
        distortion = self.get(
            "calibration.default_intrinsics.distortion", [0.0, 0.0, 0.0, 0.0, 0.0]
        )

        return CameraIntrinsics(
            width=width,
            height=height,
            fx=width * fx_ratio,
            fy=height * fy_ratio,
            ppx=width * ppx_ratio,
            ppy=height * ppy_ratio,
            distortion=distortion,
        )


def parse_resolution(resolution_str: str) -> tuple[int, int]:
    """Parse resolution string to (width, height)."""
    parts = resolution_str.lower().split("x")
    if len(parts) != 2:
        raise ValueError("Invalid resolution format")

    try:
        width, height = int(parts[0]), int(parts[1])
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"Invalid resolution format: '{resolution_str}'. "
            "Expected format: WIDTHxHEIGHT (e.g., 640x480)"
        ) from e

    if width <= 0 or height <= 0:
        raise ValueError("Dimensions must be positive")

    return (width, height)


def validate_network_config(config: dict) -> None:
    """Validate network configuration.

    Raises:
        ValueError: If configuration is invalid
    """
    if not config.get("server_ip"):
        raise ValueError(
            "Server IP not configured. "
            "Set 'network.server_ip' in config.yaml or use --host argument"
        )


# FOURCC classifications for stream type detection
FOURCC_DEPTH = {"Z16", "Y16"}
FOURCC_COLOR = {"YUYV", "YUY2", "UYVY", "MJPG", "RGB3", "BGR3"}
FOURCC_IR = {"GREY", "Y8"}
FOURCC_IR_STEREO = {"Y8I"}


def get_stream_type_from_fourcc(fourcc: str) -> str:
    """Determine stream type from FOURCC code.

    Args:
        fourcc: Four character code (e.g., "Z16", "YUYV", "Y8I")

    Returns:
        Stream type: "depth", "color", "infra", "infra_stereo", or "unknown"
    """
    fourcc_upper = fourcc.strip().upper()
    if fourcc_upper in FOURCC_DEPTH:
        return "depth"
    elif fourcc_upper in FOURCC_IR_STEREO:
        return "infra_stereo"  # Y8I 是特殊的雙紅外格式
    elif fourcc_upper in FOURCC_IR:
        return "infra"
    elif fourcc_upper in FOURCC_COLOR:
        return "color"
    return "unknown"


def format_stream_label(stream_type: str, subtype: str | None = None) -> str:
    """Format stream label with subtype information.

    Args:
        stream_type: Main stream type (depth, color, infra)
        subtype: Stream subtype (left, right for infra streams)

    Returns:
        Formatted label (e.g., "infra (LEFT)", "DEPTH")
    """
    label = stream_type.upper()
    if subtype:
        label = f"{label} ({subtype.upper()})"
    return label
