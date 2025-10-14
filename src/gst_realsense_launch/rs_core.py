#!/usr/bin/env python3
"""RealSense Streaming Core Module with Strategy Pattern.

This module implements the Strategy Pattern for different encoding and streaming approaches.
Each strategy encapsulates a specific algorithm for encoding or streaming video data.
"""

import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod


class EncoderStrategy(ABC):
    """Abstract base class for video encoding strategies.

    Different encoders (H.264, JPEG2000) implement this interface to provide
    specific encoding pipelines for GStreamer.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this encoder is available on the system."""
        pass

    @abstractmethod
    def get_pipeline_element(self, bitrate: int) -> str:
        """Get GStreamer pipeline element string for this encoder."""
        pass

    @abstractmethod
    def get_encoding_name(self) -> str:
        """Get RTP encoding name (e.g., 'H264', 'JPEG2000')."""
        pass


class NvH264EncoderStrategy(EncoderStrategy):
    """NVIDIA hardware H.264 encoder strategy (best performance on Jetson/GPU)."""

    def is_available(self) -> bool:
        """Check if nvh264enc plugin is available."""
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "nvh264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int) -> str:
        """Get NVIDIA H.264 encoder pipeline.

        Uses low-latency preset with constant bitrate for streaming.
        """
        return (
            f"nvh264enc preset=low-latency-hp bitrate={bitrate} rc-mode=cbr "
            f"! h264parse ! rtph264pay pt=96 config-interval=1"
        )

    def get_encoding_name(self) -> str:
        """Get the encoding name."""
        return "H264"


class X264EncoderStrategy(EncoderStrategy):
    """Software H.264 encoder strategy (CPU-based, widely compatible)."""

    def is_available(self) -> bool:
        """Check if x264enc plugin is available."""
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "x264enc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, bitrate: int) -> str:
        """Get software H.264 encoder pipeline.

        Uses ultrafast preset for low latency, suitable for real-time streaming.
        """
        return (
            f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} "
            f"key-int-max=30 ! h264parse ! rtph264pay pt=96 config-interval=1"
        )

    def get_encoding_name(self) -> str:
        """Get the encoding name."""
        return "H264"


class JPEG2000EncoderStrategy(EncoderStrategy):
    """JPEG2000 encoder strategy (used for depth streams to preserve bit depth)."""

    def is_available(self) -> bool:
        """Check if openjpegenc plugin is available."""
        if not shutil.which("gst-inspect-1.0"):
            return False
        result = subprocess.run(
            ["gst-inspect-1.0", "openjpegenc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    def get_pipeline_element(self, _bitrate: int = 0) -> str:
        """Get JPEG2000 encoder pipeline.

        JPEG2000 is lossless/near-lossless, preserving 16-bit depth data.
        """
        return "openjpegenc num-threads=8 ! jpeg2000parse ! rtpj2kpay pt=96"

    def get_encoding_name(self) -> str:
        """Get the encoding name."""
        return "JPEG2000"


class EncoderFactory:
    """Factory for creating encoder strategies.

    Automatically selects the best available encoder based on preference
    and system capabilities.
    """

    @staticmethod
    def create_encoder(preference: str = "auto", stream_type: str = "color") -> EncoderStrategy:
        """Create encoder strategy based on preference and stream type.

        Args:
            preference: "auto", "nvh264enc", "x264enc", or "jpeg2000"
            stream_type: "depth", "color", or "ir"

        Returns:
            Appropriate encoder strategy

        Raises:
            RuntimeError: If no suitable encoder is available
        """
        encoder: EncoderStrategy
        # Force JPEG2000 for depth streams to preserve bit depth
        if stream_type == "depth":
            encoder = JPEG2000EncoderStrategy()
            if encoder.is_available():
                return encoder
            raise RuntimeError("JPEG2000 encoder (openjpegenc) not available for depth")

        # For color/IR streams, select H.264 encoder
        if preference == "nvh264enc":
            encoder = NvH264EncoderStrategy()
            if encoder.is_available():
                return encoder
            raise RuntimeError("NVIDIA H.264 encoder (nvh264enc) not available")

        if preference == "x264enc":
            encoder = X264EncoderStrategy()
            if encoder.is_available():
                return encoder
            raise RuntimeError("Software H.264 encoder (x264enc) not available")

        # Auto selection: try NVIDIA first, then software
        if preference == "auto":
            nv_encoder = NvH264EncoderStrategy()
            if nv_encoder.is_available():
                return nv_encoder

            sw_encoder = X264EncoderStrategy()
            if sw_encoder.is_available():
                return sw_encoder

            raise RuntimeError(
                "No H.264 encoder available. Install gstreamer1.0-plugins-good "
                "or gstreamer1.0-plugins-bad"
            )

        raise ValueError(f"Unknown encoder preference: {preference}")


class StreamPipelineStrategy(ABC):
    """Abstract base class for stream pipeline building strategies.

    Different stream types (depth, color, IR) may require different
    GStreamer pipeline configurations.
    """

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
    """Strategy for depth stream (16-bit data with JPEG2000 encoding)."""

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
        bitrate: int = 4000,  # noqa: ARG002
    ) -> str:
        """Build depth stream sender pipeline.

        Depth data is 16-bit, so we use v4l2-ctl to capture raw data
        and pipe it to GStreamer with proper format parsing.
        """
        # Use v4l2-ctl to capture raw 16-bit depth data
        v4l2_cmd = (
            f"v4l2-ctl -d {shlex.quote(device)} "
            f"--set-fmt-video=width={width},height={height},pixelformat='{fourcc} ' "
            f"-p {fps} --stream-mmap --stream-to=- 2>/dev/null"
        )

        # GStreamer pipeline for encoding and transmitting
        encoder_pipeline = encoder.get_pipeline_element(bitrate=0)

        gst_cmd = (
            f"gst-launch-1.0 -e "
            f"fdsrc fd=0 do-timestamp=true "
            f"! videoparse format=gray16-le width={width} height={height} framerate={fps}/1 "
            f"! {encoder_pipeline} "
            f"! udpsink host={shlex.quote(host)} port={port} sync=false async=false"
        )

        return f"{v4l2_cmd} | {gst_cmd}"

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build depth stream receiver pipeline for gscam."""
        return (
            f"udpsrc port={port} "
            f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
            f"! rtpj2kdepay ! openjpegdec "
            f"! videoconvert ! video/x-raw,format=GRAY16_LE"
        )


class ColorStreamStrategy(StreamPipelineStrategy):
    """Strategy for color/RGB stream (H.264 encoding)."""

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
        """Build color stream sender pipeline.

        Handles different color formats (MJPEG, YUYV, etc.) and converts
        to H.264 for efficient network transmission.
        """
        fourcc_cleaned = fourcc.strip().upper()
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate)

        # Special handling for MJPEG input
        if fourcc_cleaned == "MJPG":
            return (
                f"gst-launch-1.0 -e "
                f"v4l2src device={shlex.quote(device)} do-timestamp=true "
                f"! image/jpeg,width={width},height={height},framerate={fps}/1 "
                f"! jpegdec ! videoconvert "
                f"! {encoder_pipeline} "
                f"! udpsink host={shlex.quote(host)} port={port} sync=false async=false"
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
            f"! udpsink host={shlex.quote(host)} port={port} sync=false async=false"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build color stream receiver pipeline for gscam."""
        return (
            f"udpsrc port={port} "
            f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
            f"! rtpjitterbuffer latency=50 "
            f"! rtph264depay ! avdec_h264 "
            f"! videoconvert ! video/x-raw,format=BGR"
        )


class IRStreamStrategy(StreamPipelineStrategy):
    """Strategy for infrared stream (8-bit grayscale with H.264 encoding)."""

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
        """Build IR stream sender pipeline.

        IR streams are typically 8-bit grayscale, encoded with H.264
        using lower bitrate than color.
        """
        fourcc_cleaned = fourcc.strip().upper()
        encoder_pipeline = encoder.get_pipeline_element(bitrate=bitrate)

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
            f"! udpsink host={shlex.quote(host)} port={port} sync=false async=false"
        )

    def build_receiver_pipeline(
        self,
        port: int,
        encoding: str,
    ) -> str:
        """Build IR stream receiver pipeline for gscam."""
        return (
            f"udpsrc port={port} "
            f'caps="application/x-rtp,media=video,encoding-name={encoding},payload=96" '
            f"! rtpjitterbuffer latency=50 "
            f"! rtph264depay ! avdec_h264 "
            f"! videoconvert ! video/x-raw,format=GRAY8"
        )


class StreamStrategyFactory:
    """Factory for creating stream pipeline strategies."""

    @staticmethod
    def create_strategy(stream_type: str) -> StreamPipelineStrategy:
        """Create appropriate stream strategy based on type.

        Args:
            stream_type: "depth", "color", or "ir"

        Returns:
            Appropriate stream pipeline strategy
        """
        stream_type_lower = stream_type.lower()

        if stream_type_lower == "depth":
            return DepthStreamStrategy()
        elif stream_type_lower == "color":
            return ColorStreamStrategy()
        elif stream_type_lower == "ir":
            return IRStreamStrategy()
        else:
            # Default to color strategy for unknown types
            return ColorStreamStrategy()
