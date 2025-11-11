#!/usr/bin/env python3
"""
RealSense D435i Receiver - x86 Server
Receives and decodes color, depth, and infrared streams
"""

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Dict, Optional
from threading import Thread, Lock

import cv2
import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from config import ConfigManager


class GStreamerReceiver:
    """GStreamer receiver pipeline."""
    
    def __init__(self, pipeline_str: str, name: str, callback):
        self.name = name
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.appsink = self.pipeline.get_by_name("sink")
        self.callback = callback
        self.running = False
        self.frame_count = 0
        self.last_fps_time = time.time()
        self.fps = 0.0
        
        # Setup callbacks
        self.appsink.set_property("emit-signals", True)
        self.appsink.connect("new-sample", self._on_new_sample)
        
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
    
    def start(self):
        """Start pipeline."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"Failed to start {self.name} pipeline")
        self.running = True
        self.last_fps_time = time.time()
        print(f"✓ {self.name} receiver started")
    
    def stop(self):
        """Stop pipeline."""
        if self.running:
            self.pipeline.set_state(Gst.State.NULL)
            self.running = False
            print(f"✓ {self.name} receiver stopped")
    
    def _on_new_sample(self, appsink):
        """Handle new sample."""
        sample = appsink.emit("pull-sample")
        if sample:
            buffer = sample.get_buffer()
            caps = sample.get_caps()
            
            # Extract frame info
            structure = caps.get_structure(0)
            width = structure.get_value("width")
            height = structure.get_value("height")
            format_str = structure.get_value("format")
            
            # Map buffer
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                return Gst.FlowReturn.ERROR
            
            # Convert to numpy
            if format_str == "RGB":
                frame = np.frombuffer(map_info.data, dtype=np.uint8)
                frame = frame.reshape((height, width, 3))
            elif format_str == "GRAY16_LE":
                frame = np.frombuffer(map_info.data, dtype=np.uint16)
                frame = frame.reshape((height, width))
            elif format_str == "GRAY8":
                frame = np.frombuffer(map_info.data, dtype=np.uint8)
                frame = frame.reshape((height, width))
            else:
                print(f"⚠ Unknown format: {format_str}")
                buffer.unmap(map_info)
                return Gst.FlowReturn.OK
            
            buffer.unmap(map_info)
            
            # Callback
            self.callback(self.name, frame.copy())
            
            # FPS calculation
            self.frame_count += 1
            current_time = time.time()
            elapsed = current_time - self.last_fps_time
            if elapsed >= 1.0:
                self.fps = self.frame_count / elapsed
                self.frame_count = 0
                self.last_fps_time = current_time
            
            return Gst.FlowReturn.OK
        
        return Gst.FlowReturn.ERROR
    
    def _on_message(self, bus, message):
        """Handle bus messages."""
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"✗ {self.name} Error: {err}")
            if "Could not bind to port" in str(err):
                print(f"  Hint: Port may already be in use")
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"⚠ {self.name} Warning: {warn}")
        elif t == Gst.MessageType.EOS:
            print(f"✓ {self.name} End-of-stream")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old, new, pending = message.parse_state_changed()
                if new == Gst.State.PLAYING:
                    print(f"  {self.name} → PLAYING")


class RealSenseReceiver:
    """RealSense D435i receiver."""
    
    def __init__(self, config_path: str = "config.yaml", preset: str = "balanced", display: bool = True):
        # Load configuration
        self.config = ConfigManager(config_path)
        self.preset = preset
        self.display = display
        
        # Initialize GStreamer
        Gst.init(None)
        
        # GStreamer receivers
        self.receivers: Dict[str, GStreamerReceiver] = {}
        
        # Frame storage
        self.frames = {}
        self.frame_lock = Lock()
        
        # State
        self.running = False
        self.main_loop = None
        
        # Stats
        self.start_time = None
        self.total_frames = 0
    
    def setup_receivers(self):
        """Setup GStreamer receiver pipelines."""
        preset_cfg = self.config.get_preset(self.preset)
        if not preset_cfg:
            raise ValueError(f"Preset '{self.preset}' not found")
        
        # Color receiver
        if preset_cfg.color.enabled:
            pipeline_str = self.config.build_receiver_pipeline("color", self.preset)
            self.receivers["Color"] = GStreamerReceiver(
                pipeline_str, "Color", self._on_frame
            )
        
        # Depth receiver
        if preset_cfg.depth.enabled:
            pipeline_str = self.config.build_receiver_pipeline("depth", self.preset)
            self.receivers["Depth"] = GStreamerReceiver(
                pipeline_str, "Depth", self._on_frame
            )
        
        # Infrared receivers
        if preset_cfg.infrared_stereo and preset_cfg.infrared_stereo.enabled:
            ports = preset_cfg.infrared_stereo.port
            if isinstance(ports, list):
                # Left infrared
                pipeline_left = (
                    f"udpsrc port={ports[0]} buffer-size=30000000 ! "
                    f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={preset_cfg.infrared_stereo.rtp_payload_type[0]} ! "
                    f"rtpjitterbuffer latency=50 drop-on-latency=false do-lost=true do-retransmission=false ! "
                    f"rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
                    f"queue max-size-buffers=3 leaky=downstream ! "
                    f"appsink name=sink emit-signals=true sync=false max-buffers=3 drop=true"
                )
                self.receivers["Infrared-L"] = GStreamerReceiver(
                    pipeline_left, "Infrared-L", self._on_frame
                )
                
                # Right infrared
                if len(ports) > 1:
                    pipeline_right = (
                        f"udpsrc port={ports[1]} buffer-size=30000000 ! "
                        f"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload={preset_cfg.infrared_stereo.rtp_payload_type[1]} ! "
                        f"rtpjitterbuffer latency=50 drop-on-latency=false do-lost=true do-retransmission=false ! "
                        f"rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! "
                        f"queue max-size-buffers=3 leaky=downstream ! "
                        f"appsink name=sink emit-signals=true sync=false max-buffers=3 drop=true"
                    )
                    self.receivers["Infrared-R"] = GStreamerReceiver(
                        pipeline_right, "Infrared-R", self._on_frame
                    )
        
        print(f"✓ Receivers created: {list(self.receivers.keys())}")
    
    def _on_frame(self, name: str, frame: np.ndarray):
        """Handle received frame."""
        with self.frame_lock:
            self.frames[name] = frame
            self.total_frames += 1
    
    def start(self):
        """Start receiving."""
        print("\n" + "="*80)
        print("RealSense Receiver - x86 Server")
        print("="*80)
        
        # Setup
        self.setup_receivers()
        
        # Start receivers
        time.sleep(1.0)
        for receiver in self.receivers.values():
            receiver.start()
        
        # Start main loop
        self.running = True
        self.start_time = time.time()
        
        print("\n✓ Receiving started")
        print(f"  Preset: {self.preset}")
        print(f"  Streams: {list(self.receivers.keys())}")
        
        if self.display:
            print("\nPress 'q' to quit\n")
            self._display_loop()
        else:
            print("\nPress Ctrl+C to stop\n")
            # Run GLib main loop
            self.main_loop = GLib.MainLoop()
            try:
                self.main_loop.run()
            except KeyboardInterrupt:
                print("\n\n⚠ Interrupted by user")
            finally:
                self.stop()
    
    def _display_loop(self):
        """Display frames in OpenCV windows."""
        print("Display mode active")
        
        try:
            last_stats_time = time.time()
            
            while self.running:
                with self.frame_lock:
                    frames_to_show = self.frames.copy()
                
                # Display frames
                for name, frame in frames_to_show.items():
                    if frame is None:
                        continue
                    
                    # Prepare frame for display
                    if name == "Depth":
                        # Normalize depth for visualization
                        depth_normalized = cv2.normalize(
                            frame, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U
                        )
                        depth_colored = cv2.applyColorMap(depth_normalized, cv2.COLORMAP_JET)
                        display_frame = depth_colored
                    elif name == "Color":
                        # Convert RGB to BGR for OpenCV
                        display_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    else:
                        # Infrared
                        display_frame = frame
                    
                    # Add FPS overlay
                    receiver = self.receivers.get(name)
                    if receiver:
                        fps_text = f"{name}: {receiver.fps:.1f} FPS"
                        cv2.putText(
                            display_frame, fps_text, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2
                        )
                    
                    # Show frame
                    cv2.imshow(name, display_frame)
                
                # Print stats every 5 seconds
                current_time = time.time()
                if current_time - last_stats_time >= 5.0:
                    elapsed = current_time - self.start_time
                    avg_fps = self.total_frames / elapsed if elapsed > 0 else 0
                    
                    print(f"Stats: {self.total_frames} frames total, {avg_fps:.1f} avg FPS")
                    for name, receiver in self.receivers.items():
                        print(f"  {name}: {receiver.fps:.1f} FPS")
                    
                    last_stats_time = current_time
                
                # Check for quit
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("\n⚠ Quit requested")
                    break
        
        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted by user")
        finally:
            cv2.destroyAllWindows()
            self.stop()
    
    def stop(self):
        """Stop receiving."""
        print("\n" + "="*80)
        print("Stopping...")
        print("="*80)
        
        self.running = False
        
        # Stop receivers
        for receiver in self.receivers.values():
            receiver.stop()
        
        # Stop main loop
        if self.main_loop and self.main_loop.is_running():
            self.main_loop.quit()
        
        # Stats
        if self.start_time:
            elapsed = time.time() - self.start_time
            avg_fps = self.total_frames / elapsed if elapsed > 0 else 0
            
            print(f"\nFinal Stats:")
            print(f"  Total frames: {self.total_frames}")
            print(f"  Duration: {elapsed:.1f}s")
            print(f"  Average FPS: {avg_fps:.1f}")
            
            for name, receiver in self.receivers.items():
                print(f"  {name}: {receiver.fps:.1f} FPS")
        
        print("\n✓ Receiver stopped")


def main():
    parser = argparse.ArgumentParser(description="RealSense D435i Receiver")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--preset", default="balanced",
                       choices=["high_quality", "balanced", "low_latency"],
                       help="Stream preset")
    parser.add_argument("--no-display", action="store_true", help="Disable display")
    args = parser.parse_args()
    
    # Create receiver
    receiver = RealSenseReceiver(args.config, args.preset, display=not args.no_display)
    
    # Signal handler
    def signal_handler(sig, frame):
        receiver.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start
    receiver.start()


if __name__ == "__main__":
    main()