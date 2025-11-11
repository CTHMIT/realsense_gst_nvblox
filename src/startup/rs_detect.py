import os
import re
import subprocess
from dataclasses import dataclass

import pyrealsense2 as rs  # pyre-ignore[21]: module may be missing

from gst_realsense_launch.startup.rs_common import (
    FOURCC_COLOR,
    FOURCC_DEPTH,
    FOURCC_IR,
    FOURCC_IR_STEREO,
)
from utils.logger import LOGGER


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
                            return str(devices[0].get_info(rs.camera_info.serial_number))

                        LOGGER.info(
                            "Multiple RealSense devices detected. Using first device serial."
                        )
                        return str(devices[0].get_info(rs.camera_info.serial_number))

                except Exception as e:
                    LOGGER.error(f"Could not get USB path for {dev}: {e}")

            except Exception as e:
                LOGGER.error(f"Could not query RealSense SDK for serial: {e}")

        try:
            result = cls.run_cmd(["udevadm", "info", "--query=property", f"--name={dev}"])
            for line in result.stdout.splitlines():
                if "ID_SERIAL_SHORT=" in line:
                    return str(line.split("=", 1)[1].strip())
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

    def find_best_mode(
        self,
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

    def get_best_fps(self, mode: Mode, requested: int | None = None) -> int:
        """Get best FPS for mode."""
        if requested is None:
            return max(mode.fps_list)

        valid = [f for f in mode.fps_list if f <= requested]
        if valid:
            return max(valid)

        return min(mode.fps_list, key=lambda f: abs(f - requested))
