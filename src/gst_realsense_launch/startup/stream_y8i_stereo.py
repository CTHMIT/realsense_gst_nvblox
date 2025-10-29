#!/usr/bin/env python3
"""
RealSense D435i stereo Y8I - Debug Version
Test different Y8I parsing methods
"""
import logging
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


@dataclass
class StreamConfig:
    """stream configuration"""

    width: int = 424
    height: int = 240
    fps: int = 30
    bitrate: int = 5000
    codec: str = "h264"
    preset: str = "ultrafast"
    host: str = "10.28.121.28"
    left_port: int = 5031
    right_port: int = 5032
    mtu: int = 1400
    device: str = "/dev/video2"
    display_mode: str = "window"
    display_scale: float = 1.0
    save_dir: str = ""
    parse_mode: str = "auto"  # auto, interleaved, sidebyside

    @property
    def ir_width(self) -> int:
        """single infrared width = width / 2"""
        return self.width // 2

    @property
    def expected_bytes(self) -> int:
        """Y8I bytes = width x height x 2"""
        return self.width * self.height * 2

    @property
    def estimated_bandwidth_mbps(self) -> float:
        return (self.bitrate * 2) / 1000.0

    @property
    def display_width(self) -> int:
        return int(self.ir_width * self.display_scale)

    @property
    def display_height(self) -> int:
        return int(self.height * self.display_scale)


class Y8IValidator:
    """Y8I validator"""

    SUPPORTED_Y8I_CONFIGS: dict[tuple[int, int], list[int]] = {
        (424, 240): [90, 60, 30, 15, 6],
        (480, 270): [90, 60, 30, 15, 6],
        (640, 360): [90, 60, 30, 15, 6],
        (640, 480): [90, 60, 30, 15, 6],
        (848, 100): [300, 100],
        (848, 480): [90, 60, 30, 15, 6],
        (1280, 720): [30, 15, 6],
        (1280, 800): [30, 15],
    }

    @classmethod
    def validate(cls, width: int, height: int, fps: int) -> tuple[bool, str]:
        resolution = (width, height)

        if resolution not in cls.SUPPORTED_Y8I_CONFIGS:
            available = sorted(cls.SUPPORTED_Y8I_CONFIGS.keys())
            msg_lines = [f"None support Y8I size {width}x{height}"]
            msg_lines.append("\nsupport Y8I size:")
            for w, h in available:
                ir_w = w // 2
                fps_list = cls.SUPPORTED_Y8I_CONFIGS[(w, h)]
                msg_lines.append(f"  {w}x{h} (single ir: {ir_w}x{h}) @ {fps_list} fps")
            return False, "\n".join(msg_lines)

        supported_fps = cls.SUPPORTED_Y8I_CONFIGS[resolution]
        if fps not in supported_fps:
            return False, f"Y8I {width}x{height} not support {fps} fps. need : {supported_fps}"

        ir_width = width // 2
        return True, f"config (Y8I: {width}x{height}, single ir: {ir_width}x{height})"

    @classmethod
    def get_recommended_bitrate(cls, width: int, height: int, fps: int) -> int:
        ir_width = width // 2
        pixels = ir_width * height

        if pixels <= 101760:
            base = 1500
        elif pixels <= 129600:
            base = 2000
        elif pixels <= 230400:
            base = 2500
        elif pixels <= 307200:
            base = 3500
        elif pixels <= 407040:
            base = 4500
        elif pixels <= 921600:
            base = 6000
        else:
            base = 7000

        if fps > 60:
            base = int(base * 1.5)
        elif fps > 30:
            base = int(base * 1.2)

        return base

    @classmethod
    def estimate_network_load(cls, bitrate_kbps: int, num_streams: int = 2) -> dict[str, float]:
        total_mbps = float((bitrate_kbps * num_streams) / 1000.0)
        overhead = 1.15
        actual_mbps = total_mbps * overhead

        return {
            "nominal_mbps": total_mbps,
            "with_overhead_mbps": actual_mbps,
            "gigabit_usage_percent": (actual_mbps / 1000.0) * 100,
            "100mbps_usage_percent": (actual_mbps / 100.0) * 100,
        }


class StereoIRSender:
    """stereo infrared sender"""

    def __init__(self, config: StreamConfig) -> None:
        self.config = config
        self.cap: cv2.VideoCapture | None = None
        self.gst_left: subprocess.Popen[bytes] | None = None
        self.gst_right: subprocess.Popen[bytes] | None = None
        self.running = False
        self.frame_count = 0
        self.error_count = 0
        self.start_time = 0.0

    def validate_config(self) -> bool:
        logger.info("validate config...")

        valid, msg = Y8IValidator.validate(self.config.width, self.config.height, self.config.fps)

        if not valid:
            logger.error(f"config failed:\n{msg}")
            return False

        logger.info(f"✓ {msg}")

        recommended = Y8IValidator.get_recommended_bitrate(
            self.config.width, self.config.height, self.config.fps
        )

        if self.config.bitrate < recommended * 0.5:
            logger.warning(
                f"bitrate {self.config.bitrate} kbps too low, recommended: {recommended} kbps"
            )
        elif self.config.bitrate > recommended * 2:
            logger.warning(
                f"bitrate {self.config.bitrate} kbps too high, recommended: {recommended} kbps"
            )
        else:
            logger.info(f"✓ bitrate good (recommended: {recommended} kbps)")

        network = Y8IValidator.estimate_network_load(self.config.bitrate)
        logger.info(
            f"✓ bandwidth needed: {network['nominal_mbps']:.1f} Mbps "
            + f"(with overhead: {network['with_overhead_mbps']:.1f} Mbps)"
        )

        if network["with_overhead_mbps"] > 80:
            logger.warning(f"⚠ 100 Mbps might not be enough for this stream!")
        elif network["100mbps_usage_percent"] > 70:
            logger.warning(f"⚠ 100 Mbps usage: {network['100mbps_usage_percent']:.1f}%")

        return True

    def open_camera(self) -> bool:
        logger.info(f"Opening camera {self.config.device}...")
        logger.info(f"Y8I config: {self.config.width}x{self.config.height} @ {self.config.fps} fps")
        logger.info(f"Single IR: {self.config.ir_width}x{self.config.height}")

        self.cap = cv2.VideoCapture(self.config.device, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("Y", "8", "I", " "))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.config.fps)

        if not self.cap.isOpened():
            logger.error("Cannot open camera")
            return False

        actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)

        logger.info(f"✓ Camera opened: {actual_width}x{actual_height} @ {actual_fps:.1f} fps")

        if actual_width != self.config.width or actual_height != self.config.height:
            logger.error("✗ Resolution mismatch")
            return False

        ret, test_frame = self.cap.read()
        if not ret:
            logger.error("✗ Cannot read test frame")
            return False

        logger.info(
            f"✓ Test frame: shape={test_frame.shape}, dtype={test_frame.dtype}, size={test_frame.size} bytes"
        )
        logger.info(f"  Expected: {self.config.expected_bytes} bytes (width×height×2)")

        if test_frame.size == self.config.expected_bytes:
            logger.info("  ✓ Y8I data size correct")
        else:
            logger.warning(
                f"  ⚠ Data size mismatch: got {test_frame.size}, expected {self.config.expected_bytes}"
            )

        # Debug: analyze frame structure
        self._analyze_frame_structure(test_frame)

        return True

    def _analyze_frame_structure(self, frame: np.ndarray) -> None:
        """Analyze Y8I frame structure to determine parsing method"""
        logger.info("\n=== Analyzing Y8I Frame Structure ===")
        logger.info(f"Raw frame shape: {frame.shape}")

        # Flatten frame if needed
        if len(frame.shape) > 1:
            frame_flat = frame.flatten()
            logger.info(f"Flattened to: {frame_flat.shape}")
        else:
            frame_flat = frame

        # Try different parsing methods
        methods = {}

        # Method 1: Pixel interleaved (current)
        try:
            frame_2d = frame_flat.reshape(self.config.height, self.config.width * 2)
            left_interleaved = frame_2d[:, 0::2].copy()
            right_interleaved = frame_2d[:, 1::2].copy()
            methods["interleaved"] = (left_interleaved, right_interleaved)
            logger.info(
                f"Method 1 (Pixel Interleaved): left={left_interleaved.shape}, right={right_interleaved.shape}"
            )
            logger.info(
                f"  Left mean: {left_interleaved.mean():.1f}, Right mean: {right_interleaved.mean():.1f}"
            )
        except Exception as e:
            logger.error(f"Method 1 failed: {e}")

        # Method 2: Side-by-side (horizontal split)
        try:
            frame_2d = frame_flat.reshape(self.config.height, self.config.width)
            left_sidebyside = frame_2d[:, : self.config.ir_width].copy()
            right_sidebyside = frame_2d[:, self.config.ir_width :].copy()
            methods["sidebyside"] = (left_sidebyside, right_sidebyside)
            logger.info(
                f"Method 2 (Side-by-Side): left={left_sidebyside.shape}, right={right_sidebyside.shape}"
            )
            logger.info(
                f"  Left mean: {left_sidebyside.mean():.1f}, Right mean: {right_sidebyside.mean():.1f}"
            )
        except Exception as e:
            logger.error(f"Method 2 failed: {e}")

        # Method 3: Top-bottom split (vertical)
        try:
            frame_2d = frame_flat.reshape(self.config.height * 2, self.config.ir_width)
            left_topbottom = frame_2d[: self.config.height, :].copy()
            right_topbottom = frame_2d[self.config.height :, :].copy()
            methods["topbottom"] = (left_topbottom, right_topbottom)
            logger.info(
                f"Method 3 (Top-Bottom): left={left_topbottom.shape}, right={right_topbottom.shape}"
            )
            logger.info(
                f"  Left mean: {left_topbottom.mean():.1f}, Right mean: {right_topbottom.mean():.1f}"
            )
        except Exception as e:
            logger.error(f"Method 3 failed: {e}")

        # Auto-detect best method
        if self.config.parse_mode == "auto":
            # Check if images have different mean values (indicates correct separation)
            best_method = "sidebyside"  # Default based on RealSense specs
            best_diff = 0

            for method_name, (left, right) in methods.items():
                diff = abs(left.mean() - right.mean())
                std_left = left.std()
                std_right = right.std()
                logger.info(
                    f"  {method_name}: mean_diff={diff:.1f}, std_left={std_left:.1f}, std_right={std_right:.1f}"
                )

                if diff > best_diff:
                    best_diff = diff
                    best_method = method_name

            self.config.parse_mode = best_method
            logger.info(f"\n✓ Auto-detected method: {self.config.parse_mode}")

        logger.info("=" * 40 + "\n")

    def _parse_y8i_frame(self, frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Parse Y8I frame according to selected method"""

        # Flatten frame if needed
        if len(frame.shape) > 1:
            frame_flat = frame.flatten()
        else:
            frame_flat = frame

        if self.config.parse_mode == "interleaved":
            # Pixel interleaved: [L0][R0][L1][R1]...
            # Result: each IR is full width (848 for 848x480 Y8I)
            frame_2d = frame_flat.reshape(self.config.height, self.config.width * 2)
            left_ir = frame_2d[:, 0::2].copy()  # (480, 848)
            right_ir = frame_2d[:, 1::2].copy()  # (480, 848)

        elif self.config.parse_mode == "sidebyside":
            # Side-by-side: [Left Image][Right Image]
            # Result: each IR is half width (424 for 848x480 Y8I)
            frame_2d = frame_flat.reshape(self.config.height, self.config.width)
            left_ir = frame_2d[:, : self.config.ir_width].copy()  # (480, 424)
            right_ir = frame_2d[:, self.config.ir_width :].copy()  # (480, 424)

        elif self.config.parse_mode == "topbottom":
            # Top-bottom: [Left Image on top][Right Image on bottom]
            # Result: each IR is half width (424 for 848x480 Y8I)
            frame_2d = frame_flat.reshape(self.config.height * 2, self.config.ir_width)
            left_ir = frame_2d[: self.config.height, :].copy()  # (480, 424)
            right_ir = frame_2d[self.config.height :, :].copy()  # (480, 424)

        else:
            raise ValueError(f"Unknown parse mode: {self.config.parse_mode}")

        return left_ir, right_ir

    def create_gstreamer_pipelines(self) -> bool:
        logger.info(f"Creating GStreamer pipelines (parse mode: {self.config.parse_mode})...")

        # Determine the actual IR width based on parse mode
        if self.config.parse_mode == "interleaved":
            # Interleaved mode produces full-width images
            actual_ir_width = self.config.width
        else:
            # Sidebyside and topbottom produce half-width images
            actual_ir_width = self.config.ir_width

        logger.info(f"GStreamer IR size: {actual_ir_width}x{self.config.height}")

        try:
            self.gst_left = subprocess.Popen(
                [
                    "gst-launch-1.0",
                    "-q",
                    "fdsrc",
                    "!",
                    "videoparse",
                    f"width={actual_ir_width}",
                    f"height={self.config.height}",
                    f"framerate={self.config.fps}/1",
                    "format=gray8",  # Explicit GRAY8 format
                    "!",
                    "videoconvert",
                    "!",
                    "video/x-raw,format=I420",
                    "!",
                    "x264enc",
                    "tune=zerolatency",
                    f"speed-preset={self.config.preset}",
                    f"bitrate={self.config.bitrate}",
                    f"key-int-max={self.config.fps}",
                    "!",
                    "h264parse",
                    "config-interval=1",
                    "!",
                    "rtph264pay",
                    "pt=96",
                    f"mtu={self.config.mtu}",
                    "!",
                    "udpsink",
                    f"host={self.config.host}",
                    f"port={self.config.left_port}",
                    "sync=false",
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.gst_right = subprocess.Popen(
                [
                    "gst-launch-1.0",
                    "-q",
                    "fdsrc",
                    "!",
                    "videoparse",
                    f"width={actual_ir_width}",
                    f"height={self.config.height}",
                    f"framerate={self.config.fps}/1",
                    "format=gray8",  # Explicit GRAY8 format
                    "!",
                    "videoconvert",
                    "!",
                    "video/x-raw,format=I420",
                    "!",
                    "x264enc",
                    "tune=zerolatency",
                    f"speed-preset={self.config.preset}",
                    f"bitrate={self.config.bitrate}",
                    f"key-int-max={self.config.fps}",
                    "!",
                    "h264parse",
                    "config-interval=1",
                    "!",
                    "rtph264pay",
                    "pt=97",
                    f"mtu={self.config.mtu}",
                    "!",
                    "udpsink",
                    f"host={self.config.host}",
                    f"port={self.config.right_port}",
                    "sync=false",
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            logger.info("✓ GStreamer pipelines created")
            return True

        except Exception as e:
            logger.error(f"Pipeline creation failed: {e}")
            return False

    def process_and_send(self) -> None:
        logger.info("Start streaming...")
        logger.info(f"Target: {self.config.host}:{self.config.left_port}/{self.config.right_port}")

        # Determine actual IR width based on parse mode
        if self.config.parse_mode == "interleaved":
            actual_ir_width = self.config.width
        else:
            actual_ir_width = self.config.ir_width

        logger.info(f"Single IR: {actual_ir_width}x{self.config.height} @ {self.config.fps} fps")
        logger.info(f"Bitrate: {self.config.bitrate} kbps per stream")
        logger.info(f"Parse mode: {self.config.parse_mode}")
        logger.info("Press Ctrl+C to Stop\n")

        self.running = True
        self.start_time = time.time()

        try:
            while self.running:
                ret, frame = self.cap.read()  # type: ignore[union-attr]

                if not ret:
                    self.error_count += 1
                    if self.error_count > 10:
                        logger.error("Connection lost, stopping...")
                        break
                    continue

                self.error_count = 0

                # Validate frame size
                if frame.size != self.config.expected_bytes:
                    if self.frame_count < 5:
                        logger.warning(
                            f"Frame size error: {frame.size} (expected: {self.config.expected_bytes})"
                        )
                    continue

                # Parse Y8I frame
                try:
                    left_ir, right_ir = self._parse_y8i_frame(frame)
                except Exception as e:
                    if self.frame_count < 5:
                        logger.error(f"Parse error: {e}")
                    continue

                # Verify shapes (different expected shapes for different parse modes)
                if self.config.parse_mode == "interleaved":
                    # Interleaved mode produces full-width images
                    expected_shape = (self.config.height, self.config.width)
                else:
                    # Sidebyside and topbottom produce half-width images
                    expected_shape = (self.config.height, self.config.ir_width)

                if left_ir.shape != expected_shape or right_ir.shape != expected_shape:
                    if self.frame_count < 5:
                        logger.error(
                            f"Shape error: left={left_ir.shape}, right={right_ir.shape}, "
                            f"expected={expected_shape}"
                        )
                    continue

                # Send to GStreamer
                try:
                    if self.gst_left and self.gst_left.stdin:
                        self.gst_left.stdin.write(left_ir.tobytes())
                        self.gst_left.stdin.flush()
                    if self.gst_right and self.gst_right.stdin:
                        self.gst_right.stdin.write(right_ir.tobytes())
                        self.gst_right.stdin.flush()
                except BrokenPipeError:
                    logger.error("Pipeline broken, stopping...")
                    break

                self.frame_count += 1

                if self.frame_count % self.config.fps == 0:
                    elapsed = time.time() - self.start_time
                    actual_fps = self.frame_count / elapsed if elapsed > 0 else 0

                    logger.info(
                        f"Frames: {self.frame_count:5d} | "
                        f"Time: {elapsed:5.1f}s | "
                        f"FPS: {actual_fps:5.1f} | "
                        f"Left: {left_ir.mean():5.1f} | Right: {right_ir.mean():5.1f}"
                    )

        except KeyboardInterrupt:
            logger.info("\nReceived stop signal")
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self.running = False

    def start(self) -> bool:
        if not self.validate_config():
            return False
        if not self.open_camera():
            return False
        if not self.create_gstreamer_pipelines():
            return False

        self.process_and_send()
        return True

    def stop(self) -> None:
        logger.info("Stopping...")
        self.running = False

        if self.cap:
            self.cap.release()

        for pipe in [self.gst_left, self.gst_right]:
            if pipe and pipe.stdin:
                try:
                    pipe.stdin.close()
                    pipe.terminate()
                    pipe.wait(timeout=2)
                except Exception:
                    try:
                        pipe.kill()
                    except Exception:
                        pass

        if self.start_time > 0:
            elapsed = time.time() - self.start_time
            logger.info(f"Stopped after {self.frame_count} frames in {elapsed:.1f} seconds")


class StereoIRReceiver:
    """receiver for stereo infrared"""

    def __init__(self, config: StreamConfig) -> None:
        self.config = config
        self.gst_left: subprocess.Popen[bytes] | None = None
        self.gst_right: subprocess.Popen[bytes] | None = None
        self.running = False

    def create_receiver_pipeline(self, port: int, name: str) -> subprocess.Popen[bytes]:
        payload = 96 if port == self.config.left_port else 97

        pipeline = [
            "gst-launch-1.0",
            "-q",
            "udpsrc",
            f"port={port}",
            f"caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={payload}",
            "!",
            "rtph264depay",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "videoconvert",
            "!",
        ]

        if self.config.display_scale != 1.0:
            pipeline.extend(
                [
                    "videoscale",
                    "!",
                    f"video/x-raw,width={self.config.display_width},height={self.config.display_height}",
                    "!",
                ]
            )

        if self.config.display_mode == "window":
            pipeline.extend(["autovideosink", "sync=false"])
        elif self.config.display_mode == "save":
            if self.config.save_dir:
                save_path = Path(self.config.save_dir)
            else:
                save_path = Path(tempfile.gettempdir())

            save_path.mkdir(parents=True, exist_ok=True)
            filename = save_path / f"{name}_{int(time.time())}.mp4"

            logger.info(f"Recording {name} -> {filename}")
            pipeline.extend(
                [
                    "x264enc",
                    "tune=zerolatency",
                    "!",
                    "mp4mux",
                    "!",
                    "filesink",
                    f"location={filename}",
                ]
            )
        elif self.config.display_mode == "none":
            pipeline.extend(["fakesink", "sync=false"])

        return subprocess.Popen(pipeline, stderr=subprocess.PIPE)

    def start(self) -> bool:
        logger.info("Starting receiver...")
        logger.info(f"Left IR: UDP port {self.config.left_port}")
        logger.info(f"Right IR: UDP port {self.config.right_port}")
        if self.config.display_scale != 1.0:
            logger.info(f"Display scale: {self.config.display_scale}x")
        logger.info("Press Ctrl+C to Stop\n")

        try:
            self.gst_left = self.create_receiver_pipeline(self.config.left_port, "left_ir")
            time.sleep(0.5)
            self.gst_right = self.create_receiver_pipeline(self.config.right_port, "right_ir")

            self.running = True

            while self.running:
                time.sleep(1)

                if self.gst_left and self.gst_left.poll() is not None:
                    logger.warning("Left IR stopped")
                    break
                if self.gst_right and self.gst_right.poll() is not None:
                    logger.warning("Right IR stopped")
                    break

        except KeyboardInterrupt:
            logger.info("\nReceived stop signal")
        except Exception as e:
            logger.error(f"Error: {e}")
            return False
        finally:
            self.stop()

        return True

    def stop(self) -> None:
        logger.info("Stopping receiver...")
        self.running = False

        for pipe in [self.gst_left, self.gst_right]:
            if pipe:
                try:
                    pipe.terminate()
                    pipe.wait(timeout=2)
                except Exception:
                    try:
                        pipe.kill()
                    except Exception:
                        pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="RealSense D435i Y8I stereo stream sender/receiver (Debug Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported Y8I configurations:
  424x240   @ 90/60/30/15/6 fps  (single IR: 212x240)
  848x480   @ 90/60/30/15/6 fps  (single IR: 424x480)
  1280x720  @ 30/15/6 fps        (single IR: 640x720)

Y8I Parse Modes:
  auto         - Auto-detect best parsing method (recommended)
  sidebyside   - Side-by-side format [Left][Right]
  interleaved  - Pixel interleaved [L0][R0][L1][R1]...
  topbottom    - Top-bottom format [Left on top][Right on bottom]

Examples:
  # Sender with auto-detection
  python stereo_ir_stream_debug.py send --width 848 --height 480 --fps 60

  # Sender with specific parse mode
  python stereo_ir_stream_debug.py send --parse-mode sidebyside

  # Receiver
  python stereo_ir_stream_debug.py receive --scale 0.5
        """,
    )

    parser.add_argument("mode", choices=["send", "receive"])
    parser.add_argument("--width", type=int, default=424, help="Y8I width (not single IR width)")
    parser.add_argument("--height", type=int, default=240, help="Y8I height")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=0, help="kbps per stream (0=auto)")
    parser.add_argument("--preset", default="ultrafast", help="x264 encoding preset")
    parser.add_argument("--host", default="10.28.121.28", help="receiver IP")
    parser.add_argument("--left-port", type=int, default=5031)
    parser.add_argument("--right-port", type=int, default=5032)
    parser.add_argument("--device", default="/dev/video2", help="V4L2 device")
    parser.add_argument(
        "--mode-display", dest="display_mode", default="window", choices=["window", "save", "none"]
    )
    parser.add_argument("--scale", type=float, default=1.0, help="display scale for receiver")
    parser.add_argument("--save-dir", default="", help="recording save directory (receive mode)")
    parser.add_argument(
        "--parse-mode",
        default="auto",
        choices=["auto", "sidebyside", "interleaved", "topbottom"],
        help="Y8I parsing method (sender only)",
    )

    args = parser.parse_args()

    if args.bitrate == 0:
        args.bitrate = Y8IValidator.get_recommended_bitrate(args.width, args.height, args.fps)

    config = StreamConfig(
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate=args.bitrate,
        preset=args.preset,
        host=args.host,
        left_port=args.left_port,
        right_port=args.right_port,
        device=args.device,
        display_mode=args.display_mode,
        display_scale=args.scale,
        save_dir=args.save_dir,
        parse_mode=args.parse_mode,
    )

    def signal_handler(sig: int, frame: object) -> None:
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    if args.mode == "send":
        sender = StereoIRSender(config)
        sender.start()
    else:
        receiver = StereoIRReceiver(config)
        receiver.start()


if __name__ == "__main__":
    main()
