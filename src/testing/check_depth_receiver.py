#!/usr/bin/env python3
"""Diagnostic script to verify depth receiver is working correctly

This script checks:
1. Network connectivity (can receive UDP packets on port 5020)
2. GStreamer pipeline functionality
3. ROS2 topic publication
"""

import subprocess
import sys
import time
from pathlib import Path

# ANSI color codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_header(text):
    """Print a formatted header."""
    print(f"\n{BOLD}{BLUE}{'=' * 60}{RESET}")
    print(f"{BOLD}{BLUE}{text.center(60)}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 60}{RESET}\n")


def print_success(text):
    """Print success message."""
    print(f"{GREEN}✓{RESET} {text}")


def print_error(text):
    """Print error message."""
    print(f"{RED}✗{RESET} {text}")


def print_warning(text):
    """Print warning message."""
    print(f"{YELLOW}⚠{RESET} {text}")


def print_info(text):
    """Print info message."""
    print(f"{BLUE}ℹ{RESET} {text}")


def check_port_listening(port=5020):
    """Check if any process is listening on the depth port."""
    print_header("1. Network Port Check")

    try:
        result = subprocess.run(["netstat", "-uln"], capture_output=True, text=True, timeout=5)

        if f":{port}" in result.stdout:
            print_success(f"Port {port} is in use (receiver likely running)")
            return True
        else:
            print_warning(f"Port {port} is not in use (receiver not started?)")
            return False

    except subprocess.TimeoutExpired:
        print_error("netstat command timed out")
        return False
    except FileNotFoundError:
        print_warning("netstat not available, skipping port check")
        return None


def check_udp_packets(port=5020, timeout=5):
    """Check if UDP packets are being received on the port."""
    print_header("2. UDP Packet Reception Check")

    print_info(f"Listening for packets on port {port} for {timeout} seconds...")
    print_info("Make sure sender is running!")

    try:
        # Use tcpdump to check for packets
        result = subprocess.run(
            ["timeout", str(timeout), "tcpdump", "-i", "any", "-c", "10", f"udp port {port}", "-n"],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )

        if "10 packets captured" in result.stderr or "packets captured" in result.stderr:
            lines = result.stderr.strip().split("\n")
            for line in lines:
                if "packets captured" in line:
                    print_success(f"UDP packets detected: {line}")
                    return True
            print_warning("Some packets captured but count unclear")
            return None
        else:
            print_error("No UDP packets received")
            print_info("Possible issues:")
            print_info("  • Sender not running")
            print_info("  • Firewall blocking UDP port 5020")
            print_info("  • Wrong IP address in config")
            return False

    except subprocess.TimeoutExpired:
        print_error(f"Packet capture timed out after {timeout} seconds")
        return False
    except FileNotFoundError:
        print_warning("tcpdump not available (install with: sudo apt install tcpdump)")
        print_info("Skipping packet check - you may need sudo to run tcpdump")
        return None
    except PermissionError:
        print_warning("Permission denied - tcpdump requires sudo")
        print_info("Run this script with: sudo python3 check_depth_receiver.py")
        return None


def check_gstreamer_pipeline(port=5020):
    """Test a minimal GStreamer pipeline to verify it can receive."""
    print_header("3. GStreamer Pipeline Test")

    print_info("Testing minimal GStreamer pipeline for 5 seconds...")

    pipeline = (
        f"gst-launch-1.0 -v "
        f'udpsrc port={port} caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG2000,payload=112" '
        f"! rtpjitterbuffer latency=200 "
        f"! rtpj2kdepay "
        f"! openjpegdec "
        f"! videoconvert "
        f"! video/x-raw,format=GRAY16_LE,width=640,height=480 "
        f"! fakesink"
    )

    print_info("Pipeline command:")
    print(f"  {pipeline}")

    try:
        process = subprocess.Popen(
            pipeline.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        # Let it run for 5 seconds
        time.sleep(5)
        process.terminate()

        stdout, stderr = process.communicate(timeout=2)

        # Check for success indicators
        if "Setting pipeline to PLAYING" in stderr:
            print_success("Pipeline started successfully")

            if "Handling buffer" in stderr or "buffer" in stderr.lower():
                print_success("Pipeline is receiving and processing buffers")
                return True
            else:
                print_warning("Pipeline started but no buffers detected")
                print_info("Check if sender is actually transmitting")
                return None
        else:
            print_error("Pipeline failed to start")
            print_info("Error output:")
            print(stderr[:500])
            return False

    except subprocess.TimeoutExpired:
        print_error("Pipeline test timed out")
        process.kill()
        return False
    except FileNotFoundError:
        print_error("gst-launch-1.0 not found")
        print_info("Install GStreamer: sudo apt install gstreamer1.0-tools")
        return False


def check_ros2_topics():
    """Check if ROS2 depth topics are being published."""
    print_header("4. ROS2 Topic Check")

    depth_topics = ["/camera/depth/image_rect_raw", "/camera/depth/camera_info"]

    print_info("Checking for ROS2 depth topics...")

    try:
        # List all topics
        result = subprocess.run(
            ["ros2", "topic", "list"], capture_output=True, text=True, timeout=5
        )

        topics_found = []
        topics_missing = []

        for topic in depth_topics:
            if topic in result.stdout or "depth" in result.stdout:
                print_success(f"Topic found: {topic}")
                topics_found.append(topic)
            else:
                print_warning(f"Topic not found: {topic}")
                topics_missing.append(topic)

        if topics_found:
            print_info("\nChecking topic publication rate...")
            for topic in topics_found:
                try:
                    rate_result = subprocess.run(
                        ["timeout", "3", "ros2", "topic", "hz", topic],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )

                    if "average rate" in rate_result.stdout:
                        # Extract the rate
                        for line in rate_result.stdout.split("\n"):
                            if "average rate" in line:
                                print_success(f"  {topic}: {line.strip()}")
                                break
                    else:
                        print_warning(f"  {topic}: No messages received")

                except subprocess.TimeoutExpired:
                    print_warning(f"  {topic}: Rate check timed out")

            return True
        else:
            print_error("No depth topics found")
            print_info("Receiver node may not be running or not publishing")
            return False

    except FileNotFoundError:
        print_warning("ros2 command not found")
        print_info("Source ROS2: source /opt/ros/humble/setup.bash")
        return None
    except subprocess.TimeoutExpired:
        print_error("ROS2 topic check timed out")
        return False


def check_receiver_process():
    """Check if the depth receiver process is running."""
    print_header("5. Receiver Process Check")

    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)

        if "run_depth_receiver" in result.stdout or "gst_depth_receiver" in result.stdout:
            print_success("Depth receiver process is running")

            # Show the command line
            for line in result.stdout.split("\n"):
                if "run_depth_receiver" in line or "gst_depth_receiver" in line:
                    print_info("Process details:")
                    # Extract relevant parts
                    parts = line.split()
                    print(f"  PID: {parts[1]}")
                    print(f"  Command: {' '.join(parts[10:])[:80]}...")
            return True
        else:
            print_warning("Depth receiver process not found")
            print_info("Start it with:")
            print_info(
                "  python3 run_depth_receiver.py --port 5020 --width 640 --height 480 --camera-name camera --encoding jpeg2000"
            )
            return False

    except subprocess.TimeoutExpired:
        print_error("Process check timed out")
        return False


def main():
    """Run all diagnostic checks."""
    print(f"\n{BOLD}Depth Receiver Diagnostic Tool{RESET}")
    print("This script checks if the depth receiver is properly configured and working\n")

    results = {}

    # Run all checks
    results["port"] = check_port_listening()
    results["packets"] = check_udp_packets()
    results["pipeline"] = check_gstreamer_pipeline()
    results["topics"] = check_ros2_topics()
    results["process"] = check_receiver_process()

    # Summary
    print_header("Summary")

    passed = sum(1 for v in results.values() if v is True)
    failed = sum(1 for v in results.values() if v is False)
    skipped = sum(1 for v in results.values() if v is None)

    print(f"Checks passed:  {GREEN}{passed}{RESET}")
    print(f"Checks failed:  {RED}{failed}{RESET}")
    print(f"Checks skipped: {YELLOW}{skipped}{RESET}")

    # Overall status
    if passed >= 3 and failed == 0:
        print(f"\n{GREEN}{BOLD}✓ Receiver appears to be working correctly!{RESET}")
    elif passed >= 2:
        print(f"\n{YELLOW}{BOLD}⚠ Receiver may be working but some checks failed{RESET}")
        print(f"{YELLOW}Review the warnings above{RESET}")
    else:
        print(f"\n{RED}{BOLD}✗ Receiver appears to have issues{RESET}")
        print(f"{RED}Review the errors above and check:{RESET}")
        print("  1. Is the sender actually running and transmitting?")
        print("  2. Is the receiver process started?")
        print("  3. Check firewall settings (sudo ufw status)")
        print("  4. Verify network configuration in config.yaml")

    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ["-h", "--help"]:
        print("Usage: python3 check_depth_receiver.py")
        print("\nThis script runs diagnostic checks on the depth receiver.")
        print("Some checks (tcpdump) may require sudo privileges.")
        sys.exit(0)

    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
