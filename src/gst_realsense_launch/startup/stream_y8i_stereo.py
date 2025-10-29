#!/usr/bin/env python3
"""
RealSense D435i stereo Y8I
Y8I format = width × height × 2 bytes (pixel-interleaved: L0 R0 L1 R1...)
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
    bitrate: int = 2000
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
                f"bite rate {self.config.bitrate} kbps too low, need : {recommended} kbps"
            )
        elif self.config.bitrate > recommended * 2:
            logger.warning(
                f"bite rate {self.config.bitrate} kbps too heigh, need: {recommended} kbps"
            )
        else:
            logger.info(f"✓ bite rate good for: {recommended} kbps)")

        network = Y8IValidator.estimate_network_load(self.config.bitrate)
        logger.info(
            f"✓ need more than : {network['nominal_mbps']:.1f} Mbps "
            + f"(inculde : {network['with_overhead_mbps']:.1f} Mbps)"
        )

        if network["with_overhead_mbps"] > 80:
            logger.warning(f"⚠ 100 Mbps not enough for this stream!")
        elif network["100mbps_usage_percent"] > 70:
            logger.warning(f"⚠ 100 Mbps for: {network['100mbps_usage_percent']:.1f}%")

        return True

    def open_camera(self) -> bool:
        logger.info(f"Camera {self.config.device}...")
        logger.info(f"Y8I config: {self.config.width}x{self.config.height} @ {self.config.fps} fps")
        logger.info(f"single ir: {self.config.ir_width}x{self.config.height}")

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

        logger.info(f"✓ Camera start : {actual_width}x{actual_height} @ {actual_fps:.1f} fps")

        if actual_width != self.config.width or actual_height != self.config.height:
            logger.error("✗ Size not match")
            return False

        ret, test_frame = self.cap.read()
        if not ret:
            logger.error("✗ Cannot read test frame")
            return False

        logger.info(f"✓ test frame: shape={test_frame.shape}, size={test_frame.size} bytes")
        logger.info(f"  need: {self.config.expected_bytes} bytes (width×height×2)")

        if test_frame.size == self.config.expected_bytes:
            logger.info("  ✓ Y8I data size correct")
        else:
            logger.warning("  ⚠ data size incorrect")

        return True

    def create_gstreamer_pipelines(self) -> bool:
        logger.info("Create GStreamer pipelines...")

        try:
            self.gst_left = subprocess.Popen(
                [
                    "gst-launch-1.0",
                    "-q",
                    "fdsrc",
                    "!",
                    "videoparse",
                    f"width={self.config.ir_width}",
                    f"height={self.config.height}",
                    f"framerate={self.config.fps}/1",
                    "format=2",
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
                    f"width={self.config.ir_width}",
                    f"height={self.config.height}",
                    f"framerate={self.config.fps}/1",
                    "format=2",
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
            logger.error(f"creaet failed: {e}")
            return False

    def process_and_send(self) -> None:
        logger.info("Start streaming...")
        logger.info(f"Target: {self.config.host}:{self.config.left_port}/{self.config.right_port}")
        logger.info(f"Size: {self.config.ir_width}x{self.config.height} @ {self.config.fps} fps")
        logger.info(f"byte rate: {self.config.bitrate} kbps per stream")
        logger.info("Push Ctrl+C to Stop\n")

        self.running = True
        self.start_time = time.time()

        try:
            while self.running:
                ret, frame = self.cap.read()  # type: ignore[union-attr]

                if not ret:
                    self.error_count += 1
                    if self.error_count > 10:
                        logger.error("Coneect lost, stopping...")
                        break
                    continue

                self.error_count = 0

                # Y8I format = width × height × 2 bytes
                # Pixel interleaved: [L0][R0][L1][R1]...[L423][R423] per row
                # Actual data layout: each row has width*2 bytes
                if frame.size != self.config.expected_bytes:
                    if self.frame_count < 5:
                        logger.warning(
                            f"stream data error : {frame.size} (need: {self.config.expected_bytes})"
                        )
                    continue

                # Reshape to (height, width*2), e.g., (480, 1696) for 848x480
                frame_2d = frame.reshape(self.config.height, self.config.width * 2)

                # De-interleave left and right images
                # 0::2 takes even positions (L0, L1, L2, ...)
                # 1::2 takes odd positions (R0, R1, R2, ...)
                left_ir = frame_2d[:, 0::2].copy()  # Shape: (480, 848)
                right_ir = frame_2d[:, 1::2].copy()  # Shape: (480, 848)

                # Verify de-interleaved single IR image shape
                expected_shape = (self.config.height, self.config.width)
                if left_ir.shape != expected_shape or right_ir.shape != expected_shape:
                    if self.frame_count < 5:
                        logger.error(
                            f"Shape error: left={left_ir.shape}, right={right_ir.shape}, "
                            f"expected={expected_shape}"
                        )
                    continue

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
                        f"stream:{self.frame_count:5d} | "
                        f"{elapsed:5.1f}s | "
                        f"FPS:{actual_fps:5.1f} | "
                        f"left:{left_ir.mean():5.1f} right:{right_ir.mean():5.1f}"
                    )

        except KeyboardInterrupt:
            logger.info("\nGot stop signal")
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
        logger.info("Stopping ...")
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
            logger.info(f"Stopped, program {self.frame_count} for {elapsed:.1f} secs")


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

            logger.info(f"Record {name} -> {filename}")
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
            logger.info(f"Scale: {self.config.display_scale}x")
        logger.info("Push Ctrl+C to Stop\n")

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
        logger.info("Stopping...")
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
        description="RealSense D435i Y8I stereo stream sender/receiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Supported Y8I configurations:
  424x240   @ 90/60/30/15/6 fps  (single IR: 212x240)
  848x480   @ 90/60/30/15/6 fps  (single IR: 424x480)
  1280x720  @ 30/15/6 fps        (single IR: 640x720)

Y8I Format: Pixel-interleaved [L0][R0][L1][R1]...[Ln][Rn]
  Total data: width × height × 2 bytes
  Each IR:    (width/2) × height

Examples:
  # Sender (on robot)
  python stereo_ir_stream.py send --width 848 --height 480 --fps 60

  # Receiver (on PC)
  python stereo_ir_stream.py receive --scale 0.5
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
