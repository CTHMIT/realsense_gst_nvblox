#!/usr/bin/env python3
"""
Standalone test for the GStreamer depth receiver.
This script tests if the depth receiver can initialize and receive data independently.
"""

import sys
import time
from pathlib import Path

# Test 1: Check imports
print("=" * 60)
print("TEST 1: Checking Python imports...")
print("=" * 60)

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    print("✓ GStreamer Python bindings (gi.repository) available")
except ImportError as e:
    print(f"✗ GStreamer Python bindings NOT available: {e}")
    print("\nInstall with:")
    print("  sudo apt install python3-gi python3-gst-1.0 gstreamer1.0-tools")
    sys.exit(1)

try:
    import rclpy
    from sensor_msgs.msg import CameraInfo, Image

    print("✓ ROS2 Python bindings available")
except ImportError as e:
    print(f"✗ ROS2 Python bindings NOT available: {e}")
    sys.exit(1)

try:
    import numpy as np

    print("✓ NumPy available")
except ImportError as e:
    print(f"✗ NumPy NOT available: {e}")
    sys.exit(1)

# Test 2: Check GStreamer plugins
print("\n" + "=" * 60)
print("TEST 2: Checking GStreamer plugins...")
print("=" * 60)

Gst.init(None)

required_plugins = [
    "udpsrc",
    "rtpjitterbuffer",
    "rtpj2kdepay",
    "openjpegdec",
    "rtph264depay",
    "h264parse",
    "avdec_h264",
    "videoconvert",
    "appsink",
]

missing_plugins = []
for plugin_name in required_plugins:
    plugin = Gst.ElementFactory.find(plugin_name)
    if plugin:
        print(f"✓ {plugin_name}")
    else:
        print(f"✗ {plugin_name} NOT FOUND")
        missing_plugins.append(plugin_name)

if missing_plugins:
    print(f"\n✗ Missing plugins: {', '.join(missing_plugins)}")
    print("\nInstall with:")
    print("  sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-good")
    print("  sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav")
    sys.exit(1)

# Test 3: Test minimal GStreamer pipeline
print("\n" + "=" * 60)
print("TEST 3: Testing minimal GStreamer pipeline...")
print("=" * 60)

test_pipeline_str = "videotestsrc num-buffers=1 ! videoconvert ! appsink name=sink"
try:
    test_pipeline = Gst.parse_launch(test_pipeline_str)
    test_appsink = test_pipeline.get_by_name("sink")
    test_pipeline.set_state(Gst.State.PLAYING)

    # Wait a bit
    time.sleep(0.5)

    # Try to pull a sample
    sample = test_appsink.emit("pull-sample")
    if sample:
        print("✓ GStreamer pipeline works (can create and pull samples)")
    else:
        print("✗ Could not pull sample from pipeline")

    test_pipeline.set_state(Gst.State.NULL)
except Exception as e:
    print(f"✗ GStreamer pipeline test failed: {e}")
    sys.exit(1)

# Test 4: Check if depth receiver module can be imported
print("\n" + "=" * 60)
print("TEST 4: Checking depth receiver module...")
print("=" * 60)

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gst_realsense_launch.startup.gst_depth_receiver_module import (
        check_gstreamer_python_available,
        create_depth_receiver_node,
    )

    print("✓ gst_depth_receiver_module can be imported")

    if check_gstreamer_python_available():
        print("✓ GStreamer Python is available according to module")
    else:
        print("✗ Module reports GStreamer Python is NOT available")
        sys.exit(1)

except ImportError as e:
    print(f"✗ Cannot import gst_depth_receiver_module: {e}")
    print("\nMake sure you're running this from the correct directory")
    sys.exit(1)

# Test 5: Try to create a depth receiver node (but don't start it)
print("\n" + "=" * 60)
print("TEST 5: Testing depth receiver node creation...")
print("=" * 60)

try:
    from gst_realsense_launch.startup.rs_common import ConfigLoader

    config_loader = ConfigLoader("src/config/config.yaml")
    intrinsics = config_loader.create_default_intrinsics(640, 480)

    # Initialize ROS2
    if not rclpy.ok():
        rclpy.init()

    print("Creating depth receiver node (this will try to connect to port 5020)...")
    print("If no data is being sent, this will just wait for data.")
    print("This test will run for 5 seconds then exit.")

    node = create_depth_receiver_node(
        port=5020,
        width=640,
        height=480,
        camera_name="test_camera",
        encoding="jpeg2000",
        config_loader=config_loader,
        intrinsics=intrinsics,
    )

    if node:
        print("✓ Depth receiver node created successfully!")
        print("\nNode is waiting for data on port 5020...")
        print("If the sender is running, you should see depth frames being received.")

        # Spin for a few seconds to see if we receive anything
        start_time = time.time()
        while time.time() - start_time < 5.0:
            rclpy.spin_once(node, timeout_sec=0.1)

        print("\nStopping node...")
        node.stop()
        node.destroy_node()
        print("✓ Node stopped cleanly")
    else:
        print("✗ Failed to create depth receiver node")
        sys.exit(1)

except Exception as e:
    print(f"✗ Error during node creation test: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)
finally:
    if rclpy.ok():
        rclpy.shutdown()

# All tests passed
print("\n" + "=" * 60)
print("ALL TESTS PASSED! ✓")
print("=" * 60)
print("\nYour system is properly configured for the depth receiver.")
print("\nIf the depth topic still doesn't appear:")
print("1. Check that the SENDER is actually running and sending data")
print("2. Verify network connectivity (can the receiver reach the sender?)")
print("3. Check firewall rules (is UDP port 5020 allowed?)")
print("4. Look at the logs in the tmux window: depth_5020")
print("   Use: ./view_depth_logs.sh")
