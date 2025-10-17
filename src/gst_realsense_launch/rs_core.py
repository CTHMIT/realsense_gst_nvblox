#!/usr/bin/env python3
"""RealSense Streaming Core Module with Strategy Pattern.

This module implements the Strategy Pattern for different encoding and streaming approaches.
Updated to use tested GStreamer pipelines from cmd_line.md.
"""

import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod


class EncoderStrategy(ABC):
    """Abstract base class for video encoding strategies."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this encoder is available on the system."""
        pass

    @abstractmethod
    def get_pipeline_element(self, bitrate: int | None, config: dict = None) -> str:
        """Get GStreamer pipeline element string for this encoder."""
        pass

    @abstractmethod
    def get_encoding_name(self) -> str:
        """Get RTP encoding name (e.g., 'H264', 'JPEG2000')."""
        pass


class NvH264EncoderStrategy(EncoderStrategy):
    """NVIDIA hardware H.264 encoder strategy."""

    def is_available(self) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "nvh264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int, config: dict = None) -> str:
        config = config or {}
        tune = config.get("tune", "zerolatency")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"nvh264enc preset=low-latency-hq rc-mode=cbr bitrate={bitrate} "
            f"gop-size={key_int_max} bframes=0 "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt=96"
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

    def get_pipeline_element(self, bitrate: int, config: dict = None) -> str:
        """Get software H.264 encoder pipeline with tested parameters."""
        config = config or {}
        tune = config.get("tune", "zerolatency")
        speed_preset = config.get("speed_preset", "ultrafast")
        key_int_max = config.get("key_int_max", 30)

        return (
            f"x264enc tune={tune} speed-preset={speed_preset} bitrate={bitrate} "
            f"key-int-max={key_int_max} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt=96"
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

    def get_pipeline_element(self, bitrate: int | None = None, config: dict = None) -> str:
        config = config or {}
        num_threads = config.get("num_threads", 8)

        return f"openjpegenc num-threads={num_threads} ! jpeg2000parse ! rtpj2kpay pt=96"

    def get_encoding_name(self) -> str:
        return "JPEG2000"


class EncoderFactory:
    """Factory for creating encoder strategies."""

    @staticmethod
    def create_encoder(
        preference: str = "auto", stream_type: str = "color", use_h264_for_depth: bool = False
    ) -> EncoderStrategy:
        """Create encoder strategy based on preference and stream type.

        Args:
            preference: "auto", "nvh264enc", "x264enc", or "jpeg2000"
            stream_type: "depth", "color", or "ir"
            use_h264_for_depth: If True, use H.264 for depth instead of JPEG2000
        """
        # Use H.264 for depth if configured (tested working)
        if stream_type == "depth" and use_h264_for_depth:
            if preference == "nvh264enc":
                nv_enc = NvH264EncoderStrategy()
                if nv_enc.is_available():
                    return nv_enc

            if preference == "x264enc" or preference == "auto":
                x264_enc = X264EncoderStrategy()
                if x264_enc.is_available():
                    return x264_enc

        # Original JPEG2000 for depth (preserves 16-bit)
        if stream_type == "depth" and not use_h264_for_depth:
            jpeg2k_enc = JPEG2000EncoderStrategy()
            if jpeg2k_enc.is_available():
                return jpeg2k_enc
            raise RuntimeError("JPEG2000 encoder (openjpegenc) not available for depth")

        # For color/IR streams, select H.264 encoder
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

        # Auto selection
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

        # GStreamer pipeline with H.264 encoding and RTP payload
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, config=config)

        gst_cmd = (
            f"gst-launch-1.0 -e -v fdsrc fd=0 "
            f"! videoparse format=gray16-le width={width} height={height} framerate={fps}/1 "
            f"! queue max-size-buffers=2 leaky=downstream "
            f"! videoconvert "
            f"! video/x-raw,format=I420 "
            f"! {encoder_pipeline} "
            f"! h264parse config-interval=1 "  # 關鍵: 加入 h264parse
            f"! rtph264pay pt=96 mtu=1400 "  # 關鍵: 加入 RTP payload
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
            return (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,clock-rate=90000,'
                f'encoding-name=H264,payload=96" '
                f"! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} "
                f"! rtph264depay "
                f"! h264parse "
                f"! avdec_h264 max-threads={max_threads} skip-frame=0 "
                f"! queue max-size-buffers={max_size_buffers} leaky={leaky} "
                f"! videoconvert n-threads={n_threads} "
                f"! video/x-raw,format=GRAY16_LE"
            )

        # For JPEG2000 encoded depth (16-bit preservation)
        else:
            return (
                f"udpsrc port={port} buffer-size={buffer_size} "
                f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
                f"! rtpjitterbuffer latency={latency} "
                f"! rtpj2kdepay ! openjpegdec "
                f"! videoconvert ! video/x-raw,format=GRAY16_LE"
            )


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
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, config=config)

        # Special handling for MJPEG input
        if fourcc_cleaned == "MJPG":
            return (
                f"gst-launch-1.0 -e "
                f"v4l2src device={shlex.quote(device)} do-timestamp=true "
                f"! image/jpeg,width={width},height={height},framerate={fps}/1 "
                f"! jpegdec ! videoconvert "
                f"! {encoder_pipeline} "
                f"! h264parse config-interval=1 "  # 加入 h264parse
                f"! rtph264pay pt=96 mtu=1400 "  # 加入 RTP payload
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
            f"! h264parse config-interval=1 "  # 加入 h264parse
            f"! rtph264pay pt=96 mtu=1400 "  # 加入 RTP payload
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

        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay "
            f"! h264parse "
            f"! avdec_h264 max-threads={max_threads} "
            f"! videoconvert n-threads={n_threads} "
            f"! video/x-raw,format=BGR"
        )


class Y8IStreamStrategy(StreamPipelineStrategy):
    """Strategy for Y8I interleaved infrared stream - splits into left and right."""

    def build_dual_sender_pipelines(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        encoder: EncoderStrategy,
        host: str,
        port_left: int,
        port_right: int,
        bitrate: int = 2000,
    ) -> tuple[str, str]:
        """Build pipelines to split Y8I into two separate IR streams.

        Y8I format contains left and right IR images interleaved.
        Width is 2x the actual width of each IR image.
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

        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, config=config)

        # Y8I has width = 2 * single_width
        single_width = width // 2

        # Pipeline for left IR (first half)
        pipeline_left = (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format=GRAY8,width={width},height={height},framerate={fps}/1 "
            f"! videocrop left=0 right={single_width} "  # Crop to left half
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt=96 mtu=1400 "
            f"! udpsink host={shlex.quote(host)} port={port_left} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

        # Pipeline for right IR (second half)
        pipeline_right = (
            f"gst-launch-1.0 -e "
            f"v4l2src device={shlex.quote(device)} do-timestamp=true "
            f"! video/x-raw,format=GRAY8,width={width},height={height},framerate={fps}/1 "
            f"! videocrop left={single_width} right=0 "  # Crop to right half
            f"! videoconvert "
            f"! {encoder_pipeline} "
            f"! h264parse config-interval=1 "
            f"! rtph264pay pt=96 mtu=1400 "
            f"! udpsink host={shlex.quote(host)} port={port_right} "
            f"sync={udp_config['sync']} async={udp_config['async']}"
        )

        return pipeline_left, pipeline_right


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
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate, config=config)

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
            f"! h264parse config-interval=1 "  # 加入 h264parse
            f"! rtph264pay pt=96 mtu=1400 "  # 加入 RTP payload
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

        return (
            f"udpsrc port={port} buffer-size={buffer_size} "
            f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
            f"! rtpjitterbuffer latency={latency} "
            f"! rtph264depay "
            f"! h264parse "
            f"! avdec_h264 max-threads={max_threads} "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class StreamStrategyFactory:
    """Factory for creating stream pipeline strategies."""

    @staticmethod
    def create_strategy(stream_type: str, config_loader=None) -> StreamPipelineStrategy:
        """Create appropriate stream strategy based on type.

        Args:
            stream_type: "depth", "color", or "ir"
            config_loader: Optional ConfigLoader for accessing configuration
        """
        stream_type_lower = stream_type.lower()

        if stream_type_lower == "depth":
            return DepthStreamStrategy(config_loader)
        elif stream_type_lower == "color":
            return ColorStreamStrategy(config_loader)
        elif stream_type_lower == "ir":
            return IRStreamStrategy(config_loader)
        else:
            return ColorStreamStrategy(config_loader)
