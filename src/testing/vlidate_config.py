#!/usr/bin/env python3
"""Quick configuration validation script

Validates that sender and receiver configurations match.
"""

from pathlib import Path

import yaml


def load_config(config_path="config.yaml"):
    """Load configuration file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def validate_sender_receiver_match(config, sender_log):
    """Validate sender and receiver configurations match.

    Args:
        config: Loaded config.yaml
        sender_log: Dictionary with sender log information
    """

    print("=" * 70)
    print("CONFIGURATION VALIDATION".center(70))
    print("=" * 70)

    issues = []
    warnings = []
    success = []

    # Port check
    print("\n1. PORT CONFIGURATION")
    sender_port = sender_log.get("port", 5020)
    receiver_port = config["network"]["stream_ports"]["depth"]["rtp"]

    print(f"   Sender port:   {sender_port}")
    print(f"   Receiver port: {receiver_port}")

    if sender_port == receiver_port:
        print("   ✓ Port configuration matches!")
        success.append("Port")
    else:
        print(f"   ✗ PORT MISMATCH!")
        issues.append(f"Port mismatch: sender={sender_port}, receiver={receiver_port}")

    # Encoding check
    print("\n2. ENCODING CONFIGURATION")
    sender_encoding = sender_log.get("encoding", "jpeg2000")
    receiver_encoding = config["encoding"]["depth"]["codec"]

    print(f"   Sender encoding:   {sender_encoding}")
    print(f"   Receiver encoding: {receiver_encoding}")

    if sender_encoding == receiver_encoding:
        print("   ✓ Encoding configuration matches!")
        success.append("Encoding")
    else:
        print(f"   ✗ ENCODING MISMATCH!")
        issues.append(f"Encoding mismatch: sender={sender_encoding}, receiver={receiver_encoding}")

    # Payload type check
    print("\n3. PAYLOAD TYPE")
    payload_types = config["streaming"]["rtp"]["payload_types"]

    if sender_encoding == "jpeg2000":
        expected_pt = payload_types.get("depth_jpeg2000", 112)
    elif sender_encoding == "h264":
        expected_pt = payload_types.get("depth_h264", 96)
    elif sender_encoding == "h265":
        expected_pt = payload_types.get("depth_h265", 113)
    else:
        expected_pt = None

    sender_pt = sender_log.get("payload_type", expected_pt)

    print(f"   Sender payload type:   {sender_pt}")
    print(f"   Expected payload type: {expected_pt}")

    if sender_pt == expected_pt:
        print("   ✓ Payload type is correct!")
        success.append("Payload Type")
    else:
        print(f"   ✗ PAYLOAD TYPE MISMATCH!")
        issues.append(f"Payload type mismatch: sender={sender_pt}, expected={expected_pt}")

    # Resolution check
    print("\n4. RESOLUTION")
    sender_res = sender_log.get("resolution", "640x480")
    config_res = config["camera"]["resolution"]

    print(f"   Sender resolution: {sender_res}")
    print(f"   Config resolution: {config_res}")

    if sender_res == config_res:
        print("   ✓ Resolution matches!")
        success.append("Resolution")
    else:
        print(f"   ⚠ Warning: Resolution mismatch")
        warnings.append(f"Resolution: sender={sender_res}, config={config_res}")

    # Network IP check
    print("\n5. NETWORK CONFIGURATION")
    target_ip = config["network"]["server_ip"]

    print(f"   Target IP: {target_ip}")

    if sender_log.get("target_ip"):
        sender_ip = sender_log["target_ip"]
        print(f"   Sender targeting: {sender_ip}")

        if sender_ip == target_ip:
            print("   ✓ IP addresses match!")
            success.append("Network IP")
        else:
            print(f"   ✗ IP ADDRESS MISMATCH!")
            issues.append(f"IP mismatch: sender={sender_ip}, config={target_ip}")
    else:
        print("   ℹ Sender IP not in log (using config value)")
        success.append("Network IP (from config)")

    # Frame rate check
    print("\n6. FRAME RATE")
    sender_fps = sender_log.get("fps", 30)
    config_fps = config["camera"]["fps"]

    print(f"   Sender FPS:    {sender_fps}")
    print(f"   Config FPS:    {config_fps}")

    if sender_fps == config_fps:
        print("   ✓ Frame rate matches!")
        success.append("Frame Rate")
    else:
        print(f"   ⚠ Warning: Frame rate mismatch")
        warnings.append(f"FPS: sender={sender_fps}, config={config_fps}")

    # Mode check
    print("\n7. DEPTH MODE")
    depth_mode = config["encoding"]["depth"]["mode"]
    print(f"   Depth mode: {depth_mode}")

    if depth_mode == "legacy":
        print("   ✓ Using LEGACY mode (single 16-bit stream)")
        success.append("Depth Mode")
    elif depth_mode == "split":
        print("   ⚠ Using SPLIT mode (two 8-bit streams)")
        warnings.append("Split mode uses different ports and payload types")

    # Summary
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY".center(70))
    print("=" * 70)

    print(f"\n✓ Successful checks: {len(success)}")
    for item in success:
        print(f"  • {item}")

    if warnings:
        print(f"\n⚠ Warnings: {len(warnings)}")
        for warning in warnings:
            print(f"  • {warning}")

    if issues:
        print(f"\n✗ Critical Issues: {len(issues)}")
        for issue in issues:
            print(f"  • {issue}")
        print("\n⚠ CONFIGURATION MISMATCH DETECTED!")
        print("   Sender and receiver will NOT work correctly.")
        print("   Fix the issues above before proceeding.")
        return False
    else:
        print("\n✓ ALL CRITICAL CHECKS PASSED!")
        print("   Configuration appears correct.")
        if warnings:
            print("   Some warnings exist but should not prevent operation.")
        return True


def print_receiver_pipeline(config, sender_log):
    """Print the expected receiver pipeline."""

    print("\n" + "=" * 70)
    print("EXPECTED RECEIVER PIPELINE".center(70))
    print("=" * 70)

    port = sender_log.get("port", 5020)
    width = int(sender_log.get("resolution", "640x480").split("x")[0])
    height = int(sender_log.get("resolution", "640x480").split("x")[1])
    encoding = sender_log.get("encoding", "jpeg2000")

    payload_types = config["streaming"]["rtp"]["payload_types"]
    buffer_size = config["streaming"]["udp"]["buffer_size"]
    latency = config["streaming"]["jitter_buffer"]["depth"]["latency"]
    drop_on_latency = config["streaming"]["jitter_buffer"]["depth"]["drop_on_latency"]
    max_threads = config["streaming"]["processing"]["max_threads"]

    if encoding == "jpeg2000":
        payload = payload_types.get("depth_jpeg2000", 112)
        decoder = "rtpj2kdepay ! openjpegdec"
    elif encoding == "h265":
        payload = payload_types.get("depth_h265", 113)
        decoder = f"rtph265depay ! h265parse ! avdec_h265 max-threads={max_threads}"
    else:  # h264
        payload = payload_types.get("depth_h264", 96)
        decoder = f"rtph264depay ! h264parse ! avdec_h264 max-threads={max_threads}"

    drop_str = "true" if drop_on_latency else "false"

    pipeline = f"""
udpsrc port={port} buffer-size={buffer_size} \\
  caps="application/x-rtp,media=video,clock-rate=90000,encoding-name={encoding.upper()},payload={payload}" \\
! rtpjitterbuffer latency={latency} drop-on-latency={drop_str} \\
! {decoder} \\
! queue max-size-buffers=4 leaky=downstream \\
! videoconvert n-threads=4 \\
! video/x-raw,format=GRAY16_LE,width={width},height={height},framerate=30/1 \\
! appsink name=sink emit-signals=true drop=true max-buffers=1
"""

    print("\nThis is the pipeline that will be used by the receiver:")
    print(pipeline)

    print("\nYou can test this pipeline manually with gst-launch-1.0:")
    test_pipeline = pipeline.replace(
        "appsink name=sink emit-signals=true drop=true max-buffers=1", "xvimagesink"
    )
    print(f"\ngst-launch-1.0 -v {test_pipeline.strip()}")


def main():
    """Main function."""

    # Your sender log information (from the log you provided)
    sender_log = {
        "port": 5020,
        "encoding": "jpeg2000",
        "resolution": "640x480",
        "fps": 30,
        "format": "Z16",
        "mode": "legacy",
        "target_ip": "10.28.121.28",
        "payload_type": 112,  # From pt=112 in the pipeline
    }

    try:
        # Load config
        config = load_config("src/config/config.yaml")

        # Validate
        is_valid = validate_sender_receiver_match(config, sender_log)

        # Show expected pipeline
        print_receiver_pipeline(config, sender_log)

        # Final recommendation
        print("\n" + "=" * 70)
        print("RECOMMENDATION".center(70))
        print("=" * 70)

        if is_valid:
            print("\n✓ Configuration is valid!")
            print("\nNext steps:")
            print("1. Run the diagnostic script:")
            print("   sudo python3 check_depth_receiver.py")
            print("\n2. Start the receiver:")
            print("   python3 run_depth_receiver.py \\")
            print("     --port 5020 \\")
            print("     --width 640 \\")
            print("     --height 480 \\")
            print("     --camera-name camera \\")
            print("     --encoding jpeg2000")
            print("\n3. Verify ROS2 topics:")
            print("   ros2 topic list | grep depth")
            print("   ros2 topic hz /camera/depth/image_rect_raw")
        else:
            print("\n✗ Configuration has issues!")
            print("\nFix the issues listed above, then re-run this script.")

        print("\n" + "=" * 70 + "\n")

    except FileNotFoundError:
        print("Error: config.yaml not found!")
        print("Make sure you're running this script from the correct directory.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
