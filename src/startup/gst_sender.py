#!/usr/bin/env python3
"""
RealSense D435i Sender - AGX Orin
Streams color, depth, and infrared to remote receiver
"""

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import cv2
import numpy as np
import pyrealsense2 as rs

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from config import ConfigManager


class GStreamerPipeline:
    """GStreamer pipeline wrapper."""
    
    def __init__(self, pipeline_str: str, name: str):
        self.name = name
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.appsrc = self.pipeline.get_by_name("src")
        self.running = False
        
        # Setup callbacks
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
    
    def start(self):
        """Start pipeline."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"Failed to start {self.name} pipeline")
        self.running = True
        print(f"✓ {self.name} pipeline started")
    
    def stop(self):
        """Stop pipeline."""
        if self.running:
            self.appsrc.emit("end-of-stream")
            self.pipeline.set_state(Gst.State.NULL)
            self.running = False
            print(f"✓ {self.name} pipeline stopped")
    
    def push_frame(self, frame: np.ndarray, timestamp: int):
        """Push frame to pipeline."""
        if not self.running:
            return
        
        # Create GstBuffer
        buffer = Gst.Buffer.new_wrapped(frame.tobytes())
        buffer.pts = timestamp
        buffer.dts = timestamp
        buffer.duration = Gst.CLOCK_TIME_NONE
        
        # Push buffer
        ret = self.appsrc.emit("push-buffer", buffer)
        if ret != Gst.FlowReturn.OK:
            print(f"⚠ {self.name}: push failed: {ret}")
    
    def _on_message(self, bus, message):
        """Handle bus messages."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"✗ {self.name} Error: {err}, {debug}")
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"⚠ {self.name} Warning: {warn}")
        elif t == Gst.MessageType.EOS:
            print(f"✓ {self.name} End-of-stream")


class RealSenseSender:
    """RealSense D435i sender."""
    
    def __init__(self, config_path: str = "config.yaml", preset: str = "balanced"):
        # Load configuration
        self.config = ConfigManager(config_path)
        self.preset = preset
        
        # Initialize GStreamer
        Gst.init(None)
        
        # RealSense pipeline
        self.rs_pipeline = rs.pipeline()
        self.rs_config = rs.config()
        
        # GStreamer pipelines
        self.gst_pipelines: Dict[str, GStreamerPipeline] = {}
        
        # State
        self.running = False
        self.frame_count = 0
        self.start_time = None
    
    def setup_camera(self):
        """Configure RealSense camera."""
        cam_cfg = self.config.camera
        
        # Enable streams
        if cam_cfg.streams.color:
            self.rs_config.enable_stream(
                rs.stream.color,
                cam_cfg.color_profile.width,
                cam_cfg.color_profile.height,
                rs.format.rgb8,
                cam_cfg.color_profile.fps
            )
            print(f"✓ Color: {cam_cfg.color_profile.width}x{cam_cfg.color_profile.height} @ {cam_cfg.color_profile.fps}fps")
        
        if cam_cfg.streams.depth:
            self.rs_config.enable_stream(
                rs.stream.depth,
                cam_cfg.depth_profile.width,
                cam_cfg.depth_profile.height,
                rs.format.z16,
                cam_cfg.depth_profile.fps
            )
            print(f"✓ Depth: {cam_cfg.depth_profile.width}x{cam_cfg.depth_profile.height} @ {cam_cfg.depth_profile.fps}fps")
        
        if cam_cfg.streams.infrared_stereo:
            self.rs_config.enable_stream(
                rs.stream.infrared, 1,
                cam_cfg.infrared_stereo_profile.width,
                cam_cfg.infrared_stereo_profile.height,
                rs.format.y8,
                cam_cfg.infrared_stereo_profile.fps
            )
            self.rs_config.enable_stream(
                rs.stream.infrared, 2,
                cam_cfg.infrared_stereo_profile.width,
                cam_cfg.infrared_stereo_profile.height,
                rs.format.y8,
                cam_cfg.infrared_stereo_profile.fps
            )
            print(f"✓ Infrared: {cam_cfg.infrared_stereo_profile.width}x{cam_cfg.infrared_stereo_profile.height} @ {cam_cfg.infrared_stereo_profile.fps}fps")
        
        # Start pipeline
        profile = self.rs_pipeline.start(self.rs_config)
        
        # Apply depth filters if enabled
        if cam_cfg.streams.depth and cam_cfg.depth.filters.spatial.enabled:
            print("✓ Depth filters enabled")
        
        print(f"✓ Camera initialized: {cam_cfg.model}")
    
    def setup_gstreamer(self):
        """Setup GStreamer pipelines."""
        preset_cfg = self.config.get_preset(self.preset)
        if not preset_cfg:
            raise ValueError(f"Preset '{self.preset}' not found")
        
        # Color stream
        if preset_cfg.color.enabled and self.config.camera.streams.color:
            pipeline_str = self.config.build_sender_pipeline("color", self.preset)
            self.gst_pipelines["color"] = GStreamerPipeline(pipeline_str, "Color")
        
        # Depth stream
        if preset_cfg.depth.enabled and self.config.camera.streams.depth:
            pipeline_str = self.config.build_sender_pipeline("depth", self.preset)
            self.gst_pipelines["depth"] = GStreamerPipeline(pipeline_str, "Depth")
        
        # Infrared stream (left)
        if preset_cfg.infrared_stereo and preset_cfg.infrared_stereo.enabled and self.config.camera.streams.infrared_stereo:
            # Left infrared
            ports = preset_cfg.infrared_stereo.port
            port_left = ports[0] if isinstance(ports, list) else ports
            
            # Build custom pipeline for infrared
            encoder_params = self.config.config.get_encoder_params()
            encoder_str = " ".join(f"{k}={v}" for k, v in encoder_params.items())
            
            pipeline_left = (
                f"appsrc name=src format=time is-live=true do-timestamp=true ! "
                f"video/x-raw,format=GRAY8,width={self.config.camera.infrared_stereo_profile.width},"
                f"height={self.config.camera.infrared_stereo_profile.height},"
                f"framerate={self.config.camera.infrared_stereo_profile.fps}/1 ! "
                f"videoconvert n-threads=4 ! "
                f"{encoder_str} bitrate={preset_cfg.infrared_stereo.bitrate} ! "
                f"h264parse ! "
                f"rtph264pay mtu=1400 config-interval=1 aggregate-mode=zero-latency pt={preset_cfg.infrared_stereo.rtp_payload_type[0]} ! "
                f"udpsink host={self.config.network.server_ip} port={port_left} buffer-size=30000000"
            )
            self.gst_pipelines["infra_left"] = GStreamerPipeline(pipeline_left, "Infrared-L")
            
            # Right infrared
            if len(ports) > 1:
                port_right = ports[1]
                pipeline_right = (
                    f"appsrc name=src format=time is-live=true do-timestamp=true ! "
                    f"video/x-raw,format=GRAY8,width={self.config.camera.infrared_stereo_profile.width},"
                    f"height={self.config.camera.infrared_stereo_profile.height},"
                    f"framerate={self.config.camera.infrared_stereo_profile.fps}/1 ! "
                    f"videoconvert n-threads=4 ! "
                    f"{encoder_str} bitrate={preset_cfg.infrared_stereo.bitrate} ! "
                    f"h264parse ! "
                    f"rtph264pay mtu=1400 config-interval=1 aggregate-mode=zero-latency pt={preset_cfg.infrared_stereo.rtp_payload_type[1]} ! "
                    f"udpsink host={self.config.network.server_ip} port={port_right} buffer-size=30000000"
                )
                self.gst_pipelines["infra_right"] = GStreamerPipeline(pipeline_right, "Infrared-R")
        
        print(f"✓ GStreamer pipelines created: {list(self.gst_pipelines.keys())}")
    
    def start(self):
        """Start streaming."""
        print("\n" + "="*80)
        print("RealSense Sender - AGX Orin")
        print("="*80)
        
        # Setup
        self.setup_camera()
        self.setup_gstreamer()
        
        # Start GStreamer pipelines
        time.sleep(self.config.system.timing.pipeline_startup_delay)
        for pipeline in self.gst_pipelines.values():
            pipeline.start()
        
        # Start streaming
        self.running = True
        self.start_time = time.time()
        print("\n✓ Streaming started")
        print(f"  Target: {self.config.network.server_ip}")
        print(f"  Preset: {self.preset}")
        print(f"  Streams: {list(self.gst_pipelines.keys())}")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            self._stream_loop()
        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted by user")
        finally:
            self.stop()
    
    def _stream_loop(self):
        """Main streaming loop."""
        while self.running:
            # Wait for frames
            frames = self.rs_pipeline.wait_for_frames()
            
            # Get timestamp
            timestamp = int(time.time() * 1e9)  # nanoseconds
            
            # Color frame
            if "color" in self.gst_pipelines:
                color_frame = frames.get_color_frame()
                if color_frame:
                    color_image = np.asanyarray(color_frame.get_data())
                    self.gst_pipelines["color"].push_frame(color_image, timestamp)
            
            # Depth frame
            if "depth" in self.gst_pipelines:
                depth_frame = frames.get_depth_frame()
                if depth_frame:
                    # Convert to GRAY16_LE for streaming
                    depth_image = np.asanyarray(depth_frame.get_data())
                    self.gst_pipelines["depth"].push_frame(depth_image, timestamp)
            
            # Infrared frames
            if "infra_left" in self.gst_pipelines:
                infra_left = frames.get_infrared_frame(1)
                if infra_left:
                    infra_left_image = np.asanyarray(infra_left.get_data())
                    self.gst_pipelines["infra_left"].push_frame(infra_left_image, timestamp)
            
            if "infra_right" in self.gst_pipelines:
                infra_right = frames.get_infrared_frame(2)
                if infra_right:
                    infra_right_image = np.asanyarray(infra_right.get_data())
                    self.gst_pipelines["infra_right"].push_frame(infra_right_image, timestamp)
            
            # Stats
            self.frame_count += 1
            if self.frame_count % 300 == 0:  # Every 10 seconds at 30fps
                elapsed = time.time() - self.start_time
                fps = self.frame_count / elapsed
                print(f"Stats: {self.frame_count} frames, {fps:.1f} fps, {elapsed:.1f}s")
    
    def stop(self):
        """Stop streaming."""
        print("\n" + "="*80)
        print("Stopping...")
        print("="*80)
        
        self.running = False
        
        # Stop RealSense
        if self.rs_pipeline:
            self.rs_pipeline.stop()
            print("✓ Camera stopped")
        
        # Stop GStreamer
        for pipeline in self.gst_pipelines.values():
            pipeline.stop()
        
        # Stats
        if self.start_time:
            elapsed = time.time() - self.start_time
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            print(f"\nFinal Stats:")
            print(f"  Total frames: {self.frame_count}")
            print(f"  Duration: {elapsed:.1f}s")
            print(f"  Average FPS: {fps:.1f}")
        
        print("\n✓ Sender stopped")


def main():
    parser = argparse.ArgumentParser(description="RealSense D435i Sender")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--preset", default="balanced", 
                       choices=["high_quality", "balanced", "low_latency"],
                       help="Stream preset")
    args = parser.parse_args()
    
    # Create sender
    sender = RealSenseSender(args.config, args.preset)
    
    # Signal handler
    def signal_handler(sig, frame):
        sender.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start
    sender.start()


if __name__ == "__main__":
    main()