#!/usr/bin/env python3
"""RealSense Streaming Core Module with Strategy Pattern

This module implements the Strategy Pattern for different encoding and streaming approaches.
Updated to use tested GStreamer pipelines from cmd_line.md.
"""

import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

from utils.logger import LOGGER


def get_pt(stream_type: str, encoding_name: str, config_loader=None) -> int:
    """Get RTP payload type from config or defaults."""
    if config_loader:
        payload_types: dict[str, int] = config_loader.get("streaming.rtp.payload_types", {})
        key = f"{stream_type.lower()}_{encoding_name.lower()}"
        if key in payload_types:
            return payload_types[key]

    # Fallback to hardcoded defaults
    RTP_PT = {
        ("depth", "H264"): 96,
        ("depth", "H265"): 113,
        ("depth", "JPEG2000"): 112,
        ("color", "H264"): 98,
        ("color", "H265"): 118,
        ("ir", "H264"): 97,
        ("infra_stereo", "H264"): 99,
    }
    return RTP_PT.get((stream_type.lower(), encoding_name.upper()), 96)


class EncoderStrategy(ABC):
    """Abstract base class for video encoding strategies."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this encoder is available on the system."""
        pass

    @abstractmethod
    def get_pipeline_element(self, bitrate: int | None, pt: int, config: dict = None) -> str:
        """Get GStreamer pipeline element string for this encoder."""
        pass

    @abstractmethod
    def get_encoding_name(self) -> str:
        """Get RTP encoding name (e.g., 'H264', 'H265', 'JPEG2000')."""
        pass


class NvH265EncoderStrategy(EncoderStrategy):
    """NVIDIA hardware H.265 encoder strategy."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "nvh265enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        config = config or {}
        key_int_max = config.get("key_int_max", 30)

        return (
            f"nvh265enc preset=low-latency-hq rc-mode=cbr bitrate={bitrate} "
            f"gop-size={key_int_max} bframes=0 "
            f"! h265parse config-interval=1 "
            f"! rtph265pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H265"


class X265EncoderStrategy(EncoderStrategy):
    """Software H.265 encoder strategy."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "x265enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        config = config or {}
        tune = config.get("tune", "zerolatency")
        speed_preset = config.get("speed_preset", "ultrafast")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"x265enc tune={tune} speed-preset={speed_preset} bitrate={bitrate} "
            f"key-int-max={key_int_max} "
            f"! h265parse config-interval=1 "
            f"! rtph265pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H265"


class NvH264EncoderStrategy(EncoderStrategy):
    """NVIDIA hardware H.264 encoder strategy."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "nvh264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        config = config or {}
        tune = config.get("tune", "zerolatency")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"nvh264enc preset=low-latency-hq rc-mode=cbr bitrate={bitrate} "
            f"gop-size={key_int_max} bframes=0 "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H264"


class X264EncoderStrategy(EncoderStrategy):
    """Software H.264 encoder strategy (tested and working)."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "x264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, pt: int, config: dict = None) -> str:
        """Get software H.264 encoder pipeline with tested parameters."""
        config = config or {}
        tune = config.get("tune", "zerolatency")
        speed_preset = config.get("speed_preset", "ultrafast")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"x264enc tune={tune} speed-preset={speed_preset} bitrate={bitrate} "
            f"key-int-max={key_int_max} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt={pt}"
        )

    def get_encoding_name(self) -> str:
        return "H264"


class JPEG2000EncoderStrategy(EncoderStrategy):
    """JPEG2000 encoder strategy (for 16-bit depth preservation)."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "openjpegenc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(
        self, bitrate: int | None = None, pt: int = None, config: dict = None
    ) -> str:
        config = config or {}
        num_threads = config.get("num_threads", 8)

        return f"openjpegenc num-threads={num_threads} ! jpeg2000parse ! rtpj2kpay pt={pt}"

    def get_encoding_name(self) -> str:
        return "JPEG2000"


class EncoderFactory:
    """Factory for creating encoder strategies."""

    @staticmethod
    def create_encoder(
        preference: str = "auto",
        stream_type: str = "color",
        use_h264_for_depth: bool = False,
        use_h265: bool = False,
    ) -> EncoderStrategy:
        """Create encoder strategy based on preference and stream type.

        Args:
            preference: "auto", "nvh264enc", "x264enc", "nvh265enc", or "x265enc"
            stream_type: "depth", "color", "infra_stereo" or "infra"
            use_h264_for_depth: If True, use H.264 for depth instead of JPEG2000
            use_h265: If True, prefer H.265 over H.264
        """

        # H.265 編碼器選擇
        if use_h265 or preference in ["nvh265enc", "x265enc"]:
            # 嘗試 NVIDIA H.265
            if preference in ["nvh265enc", "auto"]:
                nv_h265 = NvH265EncoderStrategy()
                if nv_h265.is_available():
                    LOGGER.info("Using NVIDIA H.265 hardware encoder")
                    return nv_h265
                elif preference == "nvh265enc":
                    # 用戶明確指定 nvh265enc 但不可用
                    LOGGER.error("NVIDIA H.265 encoder (nvh265enc) not available")
                    LOGGER.info("Please install: sudo apt install gstreamer1.0-plugins-bad")
                    LOGGER.info("Or check: gst-inspect-1.0 nvh265enc")
                    raise RuntimeError(
                        "NVIDIA H.265 encoder (nvh265enc) not available. "
                        "Install gstreamer1.0-plugins-bad or use --encoder auto"
                    )

            # 嘗試軟體 H.265
            if preference in ["x265enc", "auto"]:
                x265 = X265EncoderStrategy()
                if x265.is_available():
                    LOGGER.info("Using x265 software encoder")
                    return x265
                elif preference == "x265enc":
                    LOGGER.error("Software H.265 encoder (x265enc) not available")
                    LOGGER.info("Please install: sudo apt install gstreamer1.0-plugins-ugly")
                    LOGGER.info("Or check: gst-inspect-1.0 x265enc")
                    raise RuntimeError(
                        "Software H.265 encoder (x265enc) not available. "
                        "Install gstreamer1.0-plugins-ugly or use --encoder auto"
                    )

            # Auto 模式下，如果 H.265 都不可用，降級到 H.264
            if preference == "auto":
                LOGGER.warning("No H.265 encoder available, falling back to H.264")
            else:
                raise RuntimeError(f"H.265 encoder not available: {preference}")

        # Depth 流使用 H.264/H.265
        if stream_type == "depth" and use_h264_for_depth:
            if preference == "nvh264enc":
                nv_enc = NvH264EncoderStrategy()
                if nv_enc.is_available():
                    return nv_enc
                raise RuntimeError("NVIDIA H.264 encoder (nvh264enc) not available")

            if preference == "x264enc" or preference == "auto":
                x264_enc = X264EncoderStrategy()
                if x264_enc.is_available():
                    return x264_enc
                raise RuntimeError("Software H.264 encoder (x264enc) not available")

        # Depth 流使用 JPEG2000 (保留 16-bit)
        if stream_type == "depth" and not use_h264_for_depth:
            jpeg2k_enc = JPEG2000EncoderStrategy()
            if jpeg2k_enc.is_available():
                return jpeg2k_enc
            raise RuntimeError("JPEG2000 encoder (openjpegenc) not available for depth")

        # Color/IR 流的 H.264 編碼器選擇
        if preference == "nvh264enc":
            nv_enc = NvH264EncoderStrategy()
            if nv_enc.is_available():
                return nv_enc
            raise RuntimeError("NVIDIA H.264 encoder (nvh264enc) not available")

        if preference == "x264enc":
            x264_enc = X264EncoderStrategy()
            if x264_enc.is_available():
                return x264_enc
            raise RuntimeError("Software H.264 encoder (x264enc) not available")

        # Auto 選擇
        if preference == "auto":
            nv_enc = NvH264EncoderStrategy()
            if nv_enc.is_available():
                return nv_enc

            x264_enc = X264EncoderStrategy()
            if x264_enc.is_available():
                return x264_enc

            raise RuntimeError(
                "No H.264 encoder available. Install gstreamer1.0-plugins-good "
                "or gstreamer1.0-plugins-bad"
            )

        raise ValueError(f"Unknown encoder preference: {preference}")


class StreamPipelineStrategy(ABC):
    """Abstract base class for stream pipeline building strategies."""

    def __init__(self, config_loader=None):
        """Initialize strategy with optional config loader."""
        self.config_loader = config_loader

    @abstractmethod
    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 4000,
        is_high_byte: bool | None = None,
    ) -> str:
        """Build GStreamer pipeline for sending video stream."""
        pass

    @abstractmethod
    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build GStreamer pipeline for receiving video stream."""
        pass


class DepthStreamStrategy(StreamPipelineStrategy):
    """Strategy for depth stream (using tested v4l2-ctl + H.264 pipeline)."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 8000,
        is_high_byte: bool | None = None,
    ) -> str:
        """Build depth stream sender pipeline using tested method from cmd_line.md."""

        # Get config parameters
        config = {}
        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }
        else:
            udp_config = {"sync": "false", "async": "false"}

        # Use tested v4l2-ctl method for depth (Z16 format)
        fourcc_clean = fourcc.strip().ljust(4)

        # v4l2-ctl command to capture raw depth data
        v4l2_cmd = (
            f"v4l2-ctl -d {shlex.quote(device)} "
            f"--set-fmt-video=width={width},height={height},pixelformat='{fourcc_clean}' "
            f"--set-parm={fps} "
            f"--stream-mmap "
            f"--stream-to=- 2>/dev/null"
        )

        enc_name = encoder.get_encoding_name().upper()
        pt = get_pt("depth", enc_name)
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        gst_cmd = (
            f"gst-launch-1.0 -e -v fdsrc fd=0 "
            f"! videoparse format=gray16-le width={width} height={height} framerate={fps}/1 "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! videoconvert "
            f"! video/x-raw,format=I420 "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

        return f"{v4l2_cmd} | {gst_cmd}"

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build depth stream receiver pipeline using tested parameters."""

        # Get config parameters
        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 100)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.drop_on_latency", True
            )
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
            n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
            max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 2)
            leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        else:
            buffer_size = 2097152
            latency = 100
            drop_on_latency = True
            max_threads = 4
            n_threads = 4
            max_size_buffers = 2
            leaky = "downstream"

        drop_str = "true" if drop_on_latency else "false"

        # For H.264 encoded depth (tested working)
        if encoding.upper() == "H264":
            pt = get_pt("depth", "H264")
            return (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={pt}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format=GRAY16_LE"
            )

        # For JPEG2000 encoded depth (16-bit preservation)
        pt = get_pt("depth", "JPEG2000")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG2000,payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtpj2kdepay ! openjpegdec "
            f"! videoconvert ! video/x-raw,format=GRAY16_LE"
        )


class DepthSplitStreamStrategy(StreamPipelineStrategy):
    """Strategy for depth split mode (two 8-bit streams)."""

    def __init__(self, config_loader=None, split_mode=True):
        super().__init__(config_loader)
        self.split_mode = split_mode

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 8000,
        is_high_byte: bool = True,
    ) -> str:
        """Build depth split stream sender pipeline.

        使用 ffmpeg lut 濾鏡來分離 16-bit depth 的高低位元組：
        - 高位元組: val/256 (右移 8 位)
        - 低位元組: mod(val,256) (取低 8 位)
        """

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }
        else:
            config = {"tune": "zerolatency", "speed_preset": "ultrafast", "key_int_max": fps}
            udp_config = {"sync": "false", "async": "false"}

        fourcc_clean = fourcc.strip().ljust(4)

        # Step 1: v4l2-ctl 捕獲 RAW 16-bit depth (Z16/GRAY16_LE)
        v4l2_cmd = (
            f"v4l2-ctl -d {shlex.quote(device)} "
            f"--set-fmt-video=width={width},height={height},pixelformat='{fourcc_clean}' "
            f"--set-parm={fps} --stream-mmap --stream-to=- 2>/dev/null"
        )

        # Step 2: ffmpeg 使用 lut 濾鏡分離高低位元組
        # 高位: val/256 (等同於 >> 8)
        # 低位: mod(val,256) (等同於 & 0xFF)
        lut_expr = "val/256" if is_high_byte else "mod(val,256)"

        ffmpeg_cmd = (
            "ffmpeg -hide_banner -loglevel error "
            f"-f rawvideo -pix_fmt gray16le -s {width}x{height} -r {fps} -i - "
            f"-vf \"lut='{lut_expr}'\" "
            f"-f rawvideo -pix_fmt gray -"
        )

        # Step 3: 確定 RTP payload type
        enc_name = encoder.get_encoding_name().upper()

        if self.config_loader:
            pt_key = f"depth_{'high' if is_high_byte else 'low'}_{enc_name.lower()}"
            pt_default = 114 if is_high_byte else 115
            pt = self.config_loader.get(f"streaming.rtp.payload_types.{pt_key}", pt_default)
        else:
            pt = 114 if is_high_byte else 115

        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        # Step 4: GStreamer 編碼並透過 RTP 發送
        gst_cmd = (
            "gst-launch-1.0 -e -v fdsrc fd=0 "
            f"! videoparse format=gray8 width={width} height={height} framerate={fps}/1 "
            "! queue max-size-buffers=2 leaky=downstream "
            "! videoconvert ! video/x-raw,format=I420 "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

        # 完整管道: v4l2-ctl → ffmpeg (位元組分離) → GStreamer (編碼/RTP)
        full_pipeline = f"{v4l2_cmd} | {ffmpeg_cmd} | {gst_cmd}"

        byte_type = "HIGH" if is_high_byte else "LOW"
        LOGGER.debug(f"Depth split pipeline ({byte_type} byte):")
        LOGGER.debug(f"  1. v4l2-ctl: Capture {width}x{height} Z16 @ {fps}fps")
        LOGGER.debug(f"  2. ffmpeg: Extract {byte_type} byte using lut='{lut_expr}'")
        LOGGER.debug(f"  3. GStreamer: Encode with {enc_name} and send via RTP (PT={pt})")

        return full_pipeline

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build depth split receiver pipeline (8-bit GRAY8 output)."""

        # 這個方法不應該被呼叫，因為 split mode 的接收在 gst_receiver.py 中特別處理
        # 但為了完整性還是實現

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.depth.latency", 200)
            drop_on_latency = self.config_loader.get(
                "streaming.jitter_buffer.depth.drop_on_latency", False
            )
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
            n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
            max_size_buffers = self.config_loader.get("streaming.queue.max_size_buffers", 4)
            leaky = self.config_loader.get("streaming.queue.leaky", "downstream")
        else:
            buffer_size = 2097152
            latency = 200
            drop_on_latency = False
            max_threads = 4
            n_threads = 4
            max_size_buffers = 4
            leaky = "downstream"

        drop_str = "true" if drop_on_latency else "false"

        if encoding.upper() == "H264":
            pt = 114  # Default for depth_high_h264
            return (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={pt}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format=GRAY8"
            )
        elif encoding.upper() == "H265":
            pt = 116  # Default for depth_high_h265
            return (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload={pt}" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph265depay ! h265parse ! avdec_h265 max-threads={max_threads} "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} ! video/x-raw,format=GRAY8"
            )

        return ""


class ColorStreamStrategy(StreamPipelineStrategy):
    """Strategy for color/RGB stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 4000,
        is_high_byte: bool | None = None,
    ) -> str:
        """Build color stream sender pipeline."""

        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        fourcc_cleaned = fourcc.strip().upper()
        pt = get_pt("color", "H264")
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        # Special handling for MJPEG input
        if fourcc_cleaned == "MJPG":
            return (
                f"gst-launch-1.0 -e "
                f"v4l2src device={shlex.quote(device)} do-timestamp=true "
                f"! image/jpeg,width={width},height={height},framerate={fps}/1 "
                f"! jpegdec ! videoconvert "
                f"! {encoder_pipeline} "
                f"! udpsink host={shlex.quote(host)} port={port} "
                f"sync={udp_config['sync']} async={udp_config['async']}"
            )

        # Map FOURCC to GStreamer format
        format_map = {
            "YUYV": "YUY2",
            "YUY2": "YUY2",
            "UYVY": "UYVY",
            "GREY": "GRAY8",
            "Y8": "GRAY8",
        }
        gst_format = format_map.get(fourcc_cleaned, "YUY2")

        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format={gst_format},width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build color stream receiver pipeline with config parameters."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
            n_threads = self.config_loader.get("streaming.processing.n_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4
            n_threads = 4

        pt = get_pt("color", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert n-threads={n_threads} ! video/x-raw,format=BGR"
        )


class IRStreamStrategy(StreamPipelineStrategy):
    """Strategy for infrared stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 2000,
        is_high_byte: bool | None = None,
    ) -> str:
        """Build IR stream sender pipeline."""

        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        fourcc_cleaned = fourcc.strip().upper()
        pt = get_pt("ir", "H264")
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        format_map = {
            "GREY": "GRAY8",
            "Y8": "GRAY8",
        }
        gst_format = format_map.get(fourcc_cleaned, "GRAY8")

        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format={gst_format},width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build IR stream receiver pipeline."""

        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4

        pt = get_pt("ir", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class Y8IStreamStrategy(StreamPipelineStrategy):
    """Strategy for Y8I interleaved infrared stream."""

    def build_sender_pipeline(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        fourcc: str,
        encoder: EncoderStrategy,
        host: str,
        port: int,
        bitrate: int = 2000,
        is_high_byte: bool | None = None,
    ) -> str:
        """Build Y8I sender pipeline (sends interleaved stereo as-is).

        Y8I format has width = 2 * single_ir_width (e.g., 1280x480 for two 640x480 images)
        We send the full Y8I frame, and the receiver will split it.
        """
        config = {}
        udp_config = {"sync": "false", "async": "false"}

        if self.config_loader:
            config = {
                "tune": self.config_loader.get("encoding.h264.tune", "zerolatency"),
                "speed_preset": self.config_loader.get("encoding.h264.speed_preset", "ultrafast"),
                "key_int_max": self.config_loader.get("encoding.h264.key_int_max", fps),
            }
            udp_config = {
                "sync": str(self.config_loader.get("streaming.udp.sync", False)).lower(),
                "async": str(self.config_loader.get("streaming.udp.async", False)).lower(),
            }

        pt = get_pt("infra_stereo", "H264")
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, pt=pt, config=config)

        # Y8I is GRAY8 format but with double width
        return (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format=GRAY8,width={width},height={height},framerate={fps}/1 "
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build Y8I receiver pipeline (receives full interleaved frame).

        The receiver will get the full Y8I frame and split it in software.
        """
        if self.config_loader:
            buffer_size = self.config_loader.get("streaming.udp.buffer_size", 2097152)
            latency = self.config_loader.get("streaming.jitter_buffer.latency", 50)
            max_threads = self.config_loader.get("streaming.processing.max_threads", 4)
        else:
            buffer_size = 2097152
            latency = 50
            max_threads = 4

        pt = get_pt("infra_stereo", "H264")
        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding},payload={pt}" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class StreamStrategyFactory:
    """Factory for creating stream pipeline strategies."""

    @staticmethod
    def create_strategy(
        stream_type: str, config_loader=None, split_mode: bool = False
    ) -> StreamPipelineStrategy:
        """Create appropriate stream strategy based on type.

        Args:
            stream_type: "depth", "color", "ir", or "infra_stereo"
            config_loader: Optional ConfigLoader for accessing configuration
            split_mode: If True and stream_type is "depth", use DepthSplitStreamStrategy
        """
        stream_type_lower = stream_type.lower()

        if stream_type_lower == "depth":
            if split_mode:
                LOGGER.info("Using DepthSplitStreamStrategy for depth stream")
                return DepthSplitStreamStrategy(config_loader, split_mode=True)
            else:
                LOGGER.info("Using DepthStreamStrategy (legacy mode) for depth stream")
                return DepthStreamStrategy(config_loader)
        elif stream_type_lower == "color":
            return ColorStreamStrategy(config_loader)
        elif stream_type_lower == "infra_stereo":
            return Y8IStreamStrategy(config_loader)
        elif stream_type_lower in ["ir", "infra"]:
            return IRStreamStrategy(config_loader)
        else:
            return ColorStreamStrategy(config_loader)
