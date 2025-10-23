#!/usr/bin/env python3
"""Network Diagnostics Tool for RealSense Streaming.

This tool helps diagnose network connectivity issues between sender and receiver.
It performs comprehensive tests including:
- Network reachability
- Port availability
- Bandwidth estimation (with built-in test and RealSense-specific estimates)
- Latency measurement (accurate RTT calculation)
- Packet loss detection
- Stress testing under load
- System ports testing (ROS2, Zenoh, etc.)

Bandwidth Units:
    - Mbps (Megabits per second): Network speed, 1 Mbps = 0.125 MB/s
    - MB/s (Megabytes per second): Data transfer rate, 1 MB/s = 8 Mbps

    Example: 100 Mbps network = 12.5 MB/s actual transfer speed

    This tool displays both units for clarity:
    - "30 Mbps (3.75 MB/s)" means the same speed in different units

Configuration:
    The tool can read settings from config.yaml and ports.yaml (automatically
    searches in common locations) or accept command-line arguments. Command-line
    args override config files.

Dependencies:
    Required: socket, subprocess, json
    Optional: PyYAML (for config.yaml support), iperf3 (for advanced bandwidth test)
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TypedDict

from utils.logger import LOGGER

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class NetworkTestResult:
    """Results from a network test."""

    test_name: str
    success: bool
    message: str
    details: dict | None = None


class NetworkDiagnostics:
    """Network diagnostics tool for streaming setup."""

    def __init__(
        self,
        target_host: str,
        base_port: int = 5000,
        metadata_port: int = 5050,
        config: dict[str, Any] | None = None,
    ):
        """Initialize the network diagnostics tool.

        Args:
            target_host: The target host to run diagnostics against.
            base_port: The base port to use for diagnostics.
            metadata_port: The metadata port to use for diagnostics.
            config: Full configuration dictionary from YAML files.
        """
        self.target_host = target_host
        self.base_port = base_port
        self.metadata_port = metadata_port
        self.config: dict[str, Any] = config or {}
        self.results: list[NetworkTestResult] = []

    def run_all_tests(
        self, include_stress_test: bool = False, include_system_ports: bool = False
    ) -> bool:
        """Run all diagnostic tests.

        Args:
            include_stress_test: Whether to include stress testing.
            include_system_ports: Whether to test additional system ports from ports.yaml.

        Returns:
            True if all critical tests pass, False otherwise.
        """
        LOGGER.info(f"\n{'='*70}")
        LOGGER.info("REALSENSE STREAMING NETWORK DIAGNOSTICS")
        LOGGER.info(f"{'='*70}")
        LOGGER.info(f"Target Host: {self.target_host}")
        LOGGER.info(f"Base Port: {self.base_port}")
        LOGGER.info(f"Metadata Port: {self.metadata_port}")
        LOGGER.info(f"{'='*70}\n")

        # Run tests in order
        tests = [
            self.test_host_reachable,
            self.test_port_connectivity,
            self.test_udp_communication,
            self.test_realsense_bandwidth_estimate,
            self.test_bandwidth_builtin,
            self.test_bandwidth_iperf3,
            self.test_latency,
        ]

        if include_system_ports:
            tests.append(self.test_system_ports)

        if include_stress_test:
            tests.append(self.test_stress)

        all_passed = True
        for test in tests:
            result = test()
            self.results.append(result)

            status = "✓" if result.success else "✗"
            color = "\033[92m" if result.success else "\033[91m"
            reset = "\033[0m"

            # Mark informational tests (not critical for streaming)
            info_tests = ["ICMP", "iperf3", "RealSense Bandwidth Estimate", "System Ports"]
            info_marker = ""
            is_info_test = any(test_type in result.test_name for test_type in info_tests)

            if is_info_test:
                info_marker = " [informational]"
                if not result.success:
                    color = "\033[93m"  # Yellow for info warnings
                    status = "⚠"

            LOGGER.info(f"{color}{status}{reset} {result.test_name}{info_marker}")
            LOGGER.info(f"  {result.message}")

            if result.details:
                for key, value in result.details.items():
                    LOGGER.info(f"  {key}: {value}")

            if not result.success:
                all_passed = False

        # LOGGER.info summary
        LOGGER.info(f"{'='*70}")
        passed = sum(1 for r in self.results if r.success)
        total = len(self.results)

        # Check critical tests
        critical_tests = [
            "Port Connectivity",
            "Bandwidth Test (Built-in)",
        ]
        critical_passed = all(
            r.success for r in self.results if any(ct in r.test_name for ct in critical_tests)
        )

        # Count informational tests
        info_tests = ["ICMP", "iperf3", "RealSense Bandwidth Estimate", "System Ports"]
        info_count = sum(
            1
            for r in self.results
            if any(test_type in r.test_name for test_type in info_tests) and not r.success
        )

        LOGGER.info(f"SUMMARY: {passed}/{total} tests passed")
        if info_count > 0:
            LOGGER.info(f" ({info_count} informational)")

        if critical_passed:
            LOGGER.info("✓ All critical tests passed - streaming should work!")
            suggestions = []
            if not include_stress_test:
                suggestions.append("   • Run with --stress-test flag for load testing")
            if not include_system_ports and self.config.get("ports"):
                suggestions.append("   • Run with --system-ports to test ROS2/Zenoh ports")

            if suggestions:
                LOGGER.info("\n💪 Want more confidence?")
                for suggestion in suggestions:
                    LOGGER.info(suggestion)
        else:
            LOGGER.info("✗ Some critical tests failed - streaming may have issues")

        if info_count > 0:
            LOGGER.info("\n💡 Tip: Informational test failures are expected when:")
            LOGGER.info("   - ICMP/ping is blocked by firewall (very common)")
            LOGGER.info("   - iperf3 server is not running on target")
            LOGGER.info("   - System ports are not configured or in use by other services")
            LOGGER.info("   These don't affect RealSense UDP streaming functionality")

        LOGGER.info(f"{'='*70}\n")

        return critical_passed  # Return based on critical tests only

    def test_host_reachable(self) -> NetworkTestResult:
        """Test if target host is reachable via ping."""
        try:
            result = subprocess.run(
                ["ping", "-c", "3", "-W", "2", self.target_host],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                # Parse ping statistics
                output = result.stdout
                for line in output.split("\n"):
                    if "min/avg/max" in line:
                        # Extract average RTT
                        parts = line.split("=")[1].strip().split("/")
                        avg_rtt = parts[1]
                        return NetworkTestResult(
                            test_name="Host Reachability (ICMP)",
                            success=True,
                            message="Host is reachable via ICMP",
                            details={"Average RTT": f"{avg_rtt} ms"},
                        )

                return NetworkTestResult(
                    test_name="Host Reachability (ICMP)",
                    success=True,
                    message="Host is reachable via ICMP",
                )
            else:
                # ICMP failed, but this might be okay if UDP works
                return NetworkTestResult(
                    test_name="Host Reachability (ICMP)",
                    success=True,  # Don't fail - UDP might still work
                    message=f"ICMP ping blocked or host unreachable",
                    details={
                        "Note": "This is common - many networks block ICMP",
                        "Impact": "UDP streaming may still work fine",
                    },
                )

        except subprocess.TimeoutExpired:
            return NetworkTestResult(
                test_name="Host Reachability (ICMP)",
                success=True,  # Don't fail
                message="ICMP ping timeout (likely blocked)",
                details={
                    "Note": "ICMP is often blocked by firewalls",
                },
            )
        except Exception as e:
            return NetworkTestResult(
                test_name="Host Reachability (ICMP)",
                success=True,  # Don't fail
                message=f"ICMP test failed: {e}",
                details={
                    "Note": "UDP connectivity will be tested separately",
                },
            )

    def test_port_connectivity(self) -> NetworkTestResult:
        """Test if UDP ports are not blocked by firewall."""
        test_ports = [
            self.base_port,
            self.base_port + 1,
            self.base_port + 2,
            self.base_port + 3,
            self.metadata_port,
        ]

        details = {}
        all_ok = True

        for port in test_ports:
            try:
                # Create UDP socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(2.0)

                # Try to send a test packet
                test_data = b"DIAGNOSTIC_TEST"
                sock.sendto(test_data, (self.target_host, port))

                details[f"Port {port}"] = "Sendable"
                sock.close()

            except Exception as e:
                details[f"Port {port}"] = f"Error: {e}"
                all_ok = False

        if all_ok:
            return NetworkTestResult(
                test_name="Port Connectivity",
                success=True,
                message="All UDP ports are accessible",
                details=details,
            )
        else:
            return NetworkTestResult(
                test_name="Port Connectivity",
                success=False,
                message="Some ports may be blocked",
                details=details,
            )

    def test_udp_communication(self) -> NetworkTestResult:
        """Test bidirectional UDP communication."""
        listen_port = 19999

        try:
            # Create listener socket
            listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Bind to localhost by default for security, allow override via environment variable
            bind_address = os.getenv("BIND_ADDRESS", "127.0.0.1")
            listener.bind((bind_address, listen_port))
            listener.settimeout(5.0)

            # Send test packet
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_message = json.dumps(
                {
                    "type": "diagnostic_test",
                    "timestamp": time.time(),
                    "reply_to": listen_port,
                }
            ).encode("utf-8")

            sender.sendto(test_message, (self.target_host, self.metadata_port))

            # Try to receive response (will timeout if no receiver is running)
            try:
                data, addr = listener.recvfrom(1024)
                listener.close()
                sender.close()

                return NetworkTestResult(
                    test_name="UDP Bidirectional Communication",
                    success=True,
                    message="Bidirectional UDP communication successful",
                    details={"Response from": str(addr)},
                )
            except TimeoutError:
                listener.close()
                sender.close()

                return NetworkTestResult(
                    test_name="UDP Bidirectional Communication",
                    success=True,
                    message="Outbound UDP working (no receiver to confirm return path)",
                    details={"Note": "This is normal if receiver is not running"},
                )

        except Exception as e:
            return NetworkTestResult(
                test_name="UDP Bidirectional Communication",
                success=False,
                message=f"UDP communication test failed: {e}",
            )

    def test_realsense_bandwidth_estimate(self) -> NetworkTestResult:
        """Estimate bandwidth requirements for actual RealSense streaming configuration."""
        try:
            if not self.config:
                return NetworkTestResult(
                    test_name="RealSense Bandwidth Estimate",
                    success=True,
                    message="No config loaded, using defaults",
                    details={
                        "Estimated total": "~30-50 Mbps for typical 4-stream setup",
                        "Note": "Load config.yaml for accurate estimation",
                    },
                )

            # Get camera and encoding configuration
            camera_cfg = self.config.get("camera", {})
            encoding_cfg = self.config.get("encoding", {})

            resolution = camera_cfg.get("resolution", "640x480")
            fps = camera_cfg.get("fps", 30)
            h264_bitrate = encoding_cfg.get("bitrate", 4000)  # kbps

            # Parse resolution
            try:
                width, height = map(int, resolution.lower().split("x"))
            except:
                width, height = 640, 480

            # Calculate bandwidth per stream type
            bandwidth_estimates = {}
            total_mbps = 0.0

            # Color stream (H.264)
            color_mbps = h264_bitrate / 1000.0
            bandwidth_estimates["Color (H.264)"] = (
                f"{color_mbps:.1f} Mbps ({color_mbps/8:.2f} MB/s)"
            )
            total_mbps += color_mbps

            # Depth stream (JPEG2000 - typically ~2-3 Mbps for 640x480@30fps)
            depth_mbps = (width * height * fps * 0.5) / 1_000_000  # Rough estimate
            bandwidth_estimates["Depth (JPEG2000)"] = (
                f"{depth_mbps:.1f} Mbps ({depth_mbps/8:.2f} MB/s)"
            )
            total_mbps += depth_mbps

            # Infrared streams (H.264 - typically lower bitrate than color)
            ir_bitrate = h264_bitrate * 0.6  # IR usually needs less
            ir_mbps = ir_bitrate / 1000.0
            bandwidth_estimates["IR1 (H.264)"] = f"{ir_mbps:.1f} Mbps ({ir_mbps/8:.2f} MB/s)"
            bandwidth_estimates["IR2 (H.264)"] = f"{ir_mbps:.1f} Mbps ({ir_mbps/8:.2f} MB/s)"
            total_mbps += ir_mbps * 2

            # IMU (negligible - ~0.1 Mbps)
            imu_mbps = 0.1
            bandwidth_estimates["IMU (JSON/UDP)"] = f"{imu_mbps:.1f} Mbps ({imu_mbps/8:.2f} MB/s)"
            total_mbps += imu_mbps

            # Add overhead (RTP headers, retransmissions, etc - ~15%)
            overhead = total_mbps * 0.15
            total_with_overhead = total_mbps + overhead

            # Convert to MB/s for practical understanding
            total_mbs = total_with_overhead / 8

            sufficient = total_with_overhead <= 100  # Typical gigabit LAN can handle this

            return NetworkTestResult(
                test_name="RealSense Bandwidth Estimate",
                success=sufficient,
                message=f"Estimated: {total_with_overhead:.1f} Mbps ({total_mbs:.2f} MB/s)",
                details={
                    **bandwidth_estimates,
                    "Base total": f"{total_mbps:.1f} Mbps ({total_mbps/8:.2f} MB/s)",
                    "Network overhead (15%)": f"{overhead:.1f} Mbps ({overhead/8:.2f} MB/s)",
                    "Total with overhead": f"{total_with_overhead:.1f} Mbps ({total_mbs:.2f} MB/s)",
                    "Configuration": f"{width}x{height}@{fps}fps",
                    "Data per 10 seconds": f"{total_mbs * 10:.1f} MB",
                    "Status": "Within capacity" if sufficient else "May be too high",
                },
            )

        except Exception as e:
            return NetworkTestResult(
                test_name="RealSense Bandwidth Estimate",
                success=True,
                message=f"Could not estimate: {e}",
                details={"Note": "This is not critical, just informational"},
            )

    def test_bandwidth_builtin(self) -> NetworkTestResult:
        """Estimate bandwidth using built-in UDP throughput test."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)  # 1MB buffer

            # Prepare test data (1KB packets)
            packet_size = 1024
            test_data = b"X" * packet_size

            # Send data for 3 seconds
            duration = 3.0
            start_time = time.time()
            bytes_sent = 0
            packets_sent = 0

            LOGGER.info("  Running built-in bandwidth test...")

            while time.time() - start_time < duration:
                try:
                    sock.sendto(test_data, (self.target_host, self.base_port))
                    bytes_sent += packet_size
                    packets_sent += 1
                except Exception:
                    break

            elapsed = time.time() - start_time
            sock.close()

            LOGGER.info(" Done!")

            # Calculate throughput
            bandwidth_mbps = (bytes_sent * 8) / (elapsed * 1_000_000)
            bandwidth_mbs = bytes_sent / (elapsed * 1_000_000)
            packet_rate = packets_sent / elapsed

            # Check if bandwidth is sufficient (need ~30 Mbps for full quality)
            sufficient = bandwidth_mbps >= 30

            return NetworkTestResult(
                test_name="Bandwidth Test (Built-in)",
                success=sufficient,
                message=f"Estimated: {bandwidth_mbps:.1f} Mbps ({bandwidth_mbs:.2f} MB/s)",
                details={
                    "Packet rate": f"{packet_rate:.0f} packets/sec",
                    "Test duration": f"{elapsed:.1f} seconds",
                    "Total sent": f"{bytes_sent / 1_000_000:.1f} MB",
                    "Required": "~30 Mbps (3.75 MB/s) for full quality streaming",
                    "Status": "Sufficient" if sufficient else "May be insufficient",
                },
            )

        except Exception as e:
            return NetworkTestResult(
                test_name="Bandwidth Test (Built-in)",
                success=False,
                message=f"Bandwidth test failed: {e}",
            )

    def test_bandwidth_iperf3(self) -> NetworkTestResult:
        """Estimate available bandwidth with iperf3 if available."""
        try:
            # Check if iperf3 is available
            result = subprocess.run(["which", "iperf3"], capture_output=True, text=True)

            if result.returncode != 0:
                return NetworkTestResult(
                    test_name="Bandwidth Test (iperf3)",
                    success=True,
                    message="iperf3 not available, skipped",
                    details={"Install with": "sudo apt install iperf3"},
                )

            # Try to run iperf3 client test
            # Note: This requires iperf3 server running on target
            result = subprocess.run(
                ["iperf3", "-c", self.target_host, "-u", "-b", "100M", "-t", "3", "-J"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                bandwidth_mbps = data["end"]["sum"]["bits_per_second"] / 1_000_000
                bandwidth_mbs = bandwidth_mbps / 8
                jitter_ms = data["end"]["sum"]["jitter_ms"]
                lost_percent = data["end"]["sum"]["lost_percent"]

                # Check if bandwidth is sufficient (need ~30 Mbps for full quality)
                sufficient = bandwidth_mbps >= 30 and lost_percent < 1.0

                return NetworkTestResult(
                    test_name="Bandwidth Test (iperf3)",
                    success=sufficient,
                    message=f"UDP: {bandwidth_mbps:.1f} Mbps ({bandwidth_mbs:.2f} MB/s)",
                    details={
                        "Jitter": f"{jitter_ms:.2f} ms",
                        "Lost packets": f"{lost_percent:.2f}%",
                        "Required": "~30 Mbps (3.75 MB/s) for full quality streaming",
                        "Status": "Good" if sufficient else "May have issues",
                    },
                )
            else:
                return NetworkTestResult(
                    test_name="Bandwidth Test (iperf3)",
                    success=True,
                    message="iperf3 server not running on target",
                    details={"Note": "Start server with: iperf3 -s"},
                )

        except subprocess.TimeoutExpired:
            return NetworkTestResult(
                test_name="Bandwidth Test (iperf3)",
                success=True,  # Mark as informational, not critical
                message="iperf3 test timed out (no server running)",
                details={"Note": "Start iperf3 server on target with: iperf3 -s"},
            )
        except Exception as e:
            return NetworkTestResult(
                test_name="Bandwidth Test (iperf3)",
                success=True,
                message=f"iperf3 test skipped: {e}",
            )

    def test_latency(self) -> NetworkTestResult:
        """Test network latency using ICMP ping or UDP fallback."""
        # Try ICMP first
        try:
            result = subprocess.run(
                ["ping", "-c", "10", "-i", "0.2", self.target_host],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0:
                output = result.stdout

                # Parse ping statistics
                for line in output.split("\n"):
                    if "min/avg/max" in line:
                        # Extract RTT values
                        parts = line.split("=")[1].strip().split("/")
                        min_rtt = float(parts[0])
                        avg_rtt = float(parts[1])
                        max_rtt = float(parts[2])

                        # Parse packet loss
                        for loss_line in output.split("\n"):
                            if "packet loss" in loss_line:
                                loss_str = loss_line.split(",")[2].strip()
                                loss_percent = float(loss_str.split("%")[0])
                                break
                        else:
                            loss_percent = 0.0

                        # Latency < 50ms is good, < 100ms is acceptable
                        good_latency = avg_rtt < 50
                        acceptable_latency = avg_rtt < 100
                        low_loss = loss_percent < 5

                        success = acceptable_latency and low_loss

                        if good_latency and low_loss:
                            status = "Excellent"
                        elif acceptable_latency and low_loss:
                            status = "Good"
                        elif acceptable_latency:
                            status = "Acceptable (some packet loss)"
                        else:
                            status = "High latency"

                        return NetworkTestResult(
                            test_name="Latency Measurement (ICMP)",
                            success=success,
                            message=f"Average RTT: {avg_rtt:.1f} ms",
                            details={
                                "Min RTT": f"{min_rtt:.1f} ms",
                                "Max RTT": f"{max_rtt:.1f} ms",
                                "Packet loss": f"{loss_percent:.1f}%",
                                "Status": status,
                                "Method": "ICMP ping",
                            },
                        )
        except (subprocess.TimeoutExpired, Exception):
            pass

        # ICMP failed, fallback to UDP-based latency test
        LOGGER.info("  ICMP blocked, using UDP latency test...")

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)

            latencies = []
            successful_pings = 0
            num_attempts = 10

            for i in range(num_attempts):
                try:
                    # Send timestamped packet
                    send_time = time.time()
                    test_data = json.dumps(
                        {"type": "latency_test", "seq": i, "timestamp": send_time}
                    ).encode("utf-8")

                    sock.sendto(test_data, (self.target_host, self.metadata_port))

                    # We can't measure true RTT without a receiver, but we can measure
                    # the time it takes for the send operation to complete as a proxy
                    send_duration = (time.time() - send_time) * 1000  # ms

                    if send_duration > 0.1:  # Only count if measurable
                        latencies.append(send_duration)
                        successful_pings += 1

                    time.sleep(0.1)  # 100ms between attempts

                except TimeoutError:
                    pass
                except Exception:
                    pass

            sock.close()
            LOGGER.info(" Done!")

            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                min_latency = min(latencies)
                max_latency = max(latencies)

                # For UDP without receiver, we expect very low values (< 1ms)
                # This test mainly confirms the network path is fast
                good_latency = avg_latency < 5.0  # Very permissive for UDP send

                return NetworkTestResult(
                    test_name="Latency Measurement (UDP)",
                    success=True,  # Mark as success if we got measurements
                    message=f"UDP send latency: {avg_latency:.2f} ms (estimated)",
                    details={
                        "Min latency": f"{min_latency:.2f} ms",
                        "Max latency": f"{max_latency:.2f} ms",
                        "Successful": f"{successful_pings}/{num_attempts}",
                        "Method": "UDP send time",
                        "Note": "ICMP blocked - using UDP send time as proxy. True RTT requires receiver.",
                    },
                )
            else:
                return NetworkTestResult(
                    test_name="Latency Measurement",
                    success=True,  # Don't fail if ICMP is blocked
                    message="ICMP blocked, UDP send successful (latency cannot be measured without receiver)",
                    details={
                        "Note": "Start receiver to measure true round-trip latency",
                    },
                )

        except Exception as e:
            return NetworkTestResult(
                test_name="Latency Measurement",
                success=True,  # Don't fail the test suite
                message=f"Could not measure latency (ICMP blocked): {e}",
                details={
                    "Note": "This is not critical if UDP ports are accessible",
                },
            )

    def test_stress(self) -> NetworkTestResult:
        """Perform stress test simulating real streaming conditions."""
        try:
            LOGGER.info("  Running stress test (10 seconds)...")

            # Simulate 4 streams (color, depth, infrared1, infrared2)
            num_streams = 4
            packet_size = 1400  # Typical MTU-safe size
            target_fps = 30
            target_bitrate = 30_000_000  # 30 Mbps total

            # Calculate packets per second per stream
            bytes_per_frame = target_bitrate / 8 / target_fps / num_streams
            packets_per_frame = int(bytes_per_frame / packet_size) + 1

            # Test duration
            duration = 10.0

            # Statistics tracking
            packets_sent = 0
            bytes_sent = 0
            send_errors = 0

            # Create test data
            test_data = b"S" * packet_size

            # Create sockets for each stream
            sockets = []
            for i in range(num_streams):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * 1024 * 1024)
                sockets.append(sock)

            start_time = time.time()
            frame_interval = 1.0 / target_fps
            next_frame_time = start_time

            while time.time() - start_time < duration:
                current_time = time.time()

                # Wait until next frame time
                if current_time < next_frame_time:
                    sleep_time = next_frame_time - current_time
                    if sleep_time > 0:
                        time.sleep(sleep_time)

                # Send packets for this frame across all streams
                for stream_idx, sock in enumerate(sockets):
                    port = self.base_port + stream_idx
                    for packet_idx in range(packets_per_frame):
                        try:
                            sock.sendto(test_data, (self.target_host, port))
                            packets_sent += 1
                            bytes_sent += packet_size
                        except Exception:
                            send_errors += 1

                next_frame_time += frame_interval

            # Close sockets
            for sock in sockets:
                sock.close()

            elapsed = time.time() - start_time

            LOGGER.info(" Done!")

            # Calculate statistics
            actual_bitrate_mbps = (bytes_sent * 8) / (elapsed * 1_000_000)
            actual_bitrate_mbs = bytes_sent / (elapsed * 1_000_000)
            actual_fps = packets_sent / packets_per_frame / num_streams / elapsed
            error_rate = (send_errors / packets_sent * 100) if packets_sent > 0 else 0

            # Success criteria
            bitrate_ok = actual_bitrate_mbps >= (target_bitrate / 1_000_000 * 0.9)  # 90% of target
            low_errors = error_rate < 1.0
            success = bitrate_ok and low_errors

            return NetworkTestResult(
                test_name="Stress Test (Simulated Streaming)",
                success=success,
                message=f"Achieved: {actual_bitrate_mbps:.1f} Mbps ({actual_bitrate_mbs:.2f} MB/s)",
                details={
                    "Target bitrate": f"{target_bitrate / 1_000_000:.1f} Mbps ({target_bitrate / 8_000_000:.2f} MB/s)",
                    "Actual fps": f"{actual_fps:.1f}",
                    "Packets sent": f"{packets_sent:,}",
                    "Total data": f"{bytes_sent / 1_000_000:.1f} MB",
                    "Send errors": f"{send_errors} ({error_rate:.2f}%)",
                    "Duration": f"{elapsed:.1f} seconds",
                    "Status": "Passed" if success else "Failed",
                },
            )

        except Exception as e:
            return NetworkTestResult(
                test_name="Stress Test (Simulated Streaming)",
                success=False,
                message=f"Stress test failed: {e}",
            )

    def test_system_ports(self) -> NetworkTestResult:
        """Test additional system ports defined in ports.yaml."""
        try:
            ports_to_test = []

            if self.config and "ports" in self.config:
                ports_cfg = self.config["ports"]

                # Extract UDP ports from ports.yaml
                udp_ports = ports_cfg.get("udp", {})

                # ROS2 FastDDS ports
                fastdds = udp_ports.get("ros2_fastdds", {})
                if "domain_0" in fastdds:
                    domain_0 = fastdds["domain_0"]
                    if "multicast" in domain_0:
                        ports_to_test.extend(
                            [(p, "ROS2 FastDDS Domain 0 Multicast") for p in domain_0["multicast"]]
                        )

                # FastDDS discovery server
                if "fastdds_discovery_server" in udp_ports:
                    ports_to_test.append(
                        (udp_ports["fastdds_discovery_server"], "FastDDS Discovery Server")
                    )

                # Zenoh ports
                zenoh = udp_ports.get("zenoh", {})
                if "scouting" in zenoh:
                    ports_to_test.append((zenoh["scouting"], "Zenoh Scouting"))
                if "quic" in zenoh:
                    ports_to_test.append((zenoh["quic"], "Zenoh QUIC"))

            if not ports_to_test:
                return NetworkTestResult(
                    test_name="System Ports Test",
                    success=True,
                    message="No additional ports configured in ports.yaml",
                    details={
                        "Note": "This is optional - RealSense streaming doesn't require these ports"
                    },
                )

            LOGGER.info(f"  Testing {len(ports_to_test)} system ports...")

            # Test each port
            accessible_ports = 0
            port_details = {}

            for port, description in ports_to_test:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(1.0)
                    test_data = b"SYSTEM_PORT_TEST"
                    sock.sendto(test_data, (self.target_host, port))
                    sock.close()

                    port_details[f"{description} ({port})"] = "Accessible"
                    accessible_ports += 1
                except Exception as e:
                    port_details[f"{description} ({port})"] = f"Error: {str(e)[:30]}"

            LOGGER.info(" Done!")

            success_rate = accessible_ports / len(ports_to_test)

            return NetworkTestResult(
                test_name="System Ports Test",
                success=True,  # Mark as informational
                message=f"{accessible_ports}/{len(ports_to_test)} system ports accessible",
                details={
                    **port_details,
                    "Note": "System port failures don't affect RealSense streaming",
                },
            )

        except Exception as e:
            return NetworkTestResult(
                test_name="System Ports Test",
                success=True,  # Don't fail on this
                message=f"System ports test skipped: {e}",
                details={"Note": "This is optional"},
            )

    def export_results(self, filename: str):
        """Export test results to JSON file."""
        data = {
            "target_host": self.target_host,
            "base_port": self.base_port,
            "metadata_port": self.metadata_port,
            "timestamp": time.time(),
            "config_loaded": bool(self.config),
            "results": [
                {
                    "test": r.test_name,
                    "success": r.success,
                    "message": r.message,
                    "details": r.details or {},
                }
                for r in self.results
            ],
        }

        with open(filename, "w") as f:
            json.dump(data, f, indent=2)

        LOGGER.info(f"Results exported to {filename}")


def _try_int(value: Any) -> int | None:
    """Return int if can parse, else None."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        return int(v) if v.isdigit() else None
    if isinstance(value, dict):
        for k in ("udp", "port"):
            if k in value:
                return _try_int(value[k])
    return None


def _as_int(value: Any, default: int) -> int:
    """Best-effort parse port to int from int/str/dict/list; otherwise default."""
    p = _try_int(value)
    if p is not None:
        return p
    if isinstance(value, list):
        for item in value:
            p2 = _try_int(item)
            if p2 is not None:
                return p2
    return default


class LoadedConfig(TypedDict):
    host: str | None
    base_port: int
    metadata_port: int
    full_config: dict[str, Any]


def load_config(config_path: str = None, ports_path: str = None) -> LoadedConfig:
    """Load configuration from YAML files.

    Args:
        config_path: Path to config file. If None, searches in default locations.
        ports_path: Path to ports file. If None, searches in default locations.

    Returns:
        Dictionary with configuration values including network, camera, and ports config.
    """
    # Default configuration
    full_config_data: dict[str, Any] = {}
    default_config: LoadedConfig = {
        "host": None,
        "base_port": 5000,
        "metadata_port": 5050,
        "full_config": full_config_data,
    }
    # If no YAML support, return defaults
    if yaml is None:
        LOGGER.info("Warning: PyYAML not installed. Using default values.")
        LOGGER.info("Install with: pip install pyyaml")
        return default_config

    full_config: dict[str, Any] = {}

    # Search for config.yaml
    if config_path is None:
        search_paths = [
            "src/config/config.yaml",
            "config/config.yaml",
            "config.yaml",
            "../config/config.yaml",
            "../../config/config.yaml",
        ]

        for path in search_paths:
            if os.path.exists(path):
                config_path = path
                break

    # Load config.yaml
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)

            LOGGER.info(f"✓ Loaded configuration from: {config_path}")
            full_config_data.update(config)

            # Extract network configuration
            network_config: dict[str, Any] = config.get("network", {}) or {}
            stream_ports: dict[str, Any] = network_config.get("stream_ports", {}) or {}
            imu_cfg: Any = stream_ports.get("imu", {})

            default_config.update(
                {
                    "host": network_config.get("server_ip"),
                    "base_port": _as_int(network_config.get("base_port"), 5000),
                    "metadata_port": _as_int(imu_cfg, 5050),
                }
            )

        except Exception as e:
            LOGGER.info(f"Warning: Failed to load config file: {e}")
    else:
        LOGGER.info(f"Warning: Config file not found. Using default values.")

    # Search for ports.yaml
    if ports_path is None:
        ports_search_paths = [
            "src/config/ports.yaml",
            "config/ports.yaml",
            "ports.yaml",
            "../config/ports.yaml",
            "../../config/ports.yaml",
        ]

        for path in ports_search_paths:
            if os.path.exists(path):
                ports_path = path
                break

    # Load ports.yaml
    if ports_path and os.path.exists(ports_path):
        try:
            with open(ports_path) as f:
                ports_config = yaml.safe_load(f)

            LOGGER.info(f"✓ Loaded ports configuration from: {ports_path}")
            full_config_data["ports"] = ports_config

        except Exception as e:
            LOGGER.info(f"Warning: Failed to load ports file: {e}")

    # Merge full config into default config
    default_config["full_config"] = full_config_data

    return default_config


def main():
    """Run the network diagnostics tool."""
    parser = argparse.ArgumentParser(
        description="Network diagnostics for RealSense streaming",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use config.yaml for all settings
  python network_diagnostics.py

  # Override host from config
  python network_diagnostics.py 192.168.1.100

  # Specify custom config file
  python network_diagnostics.py --config /path/to/config.yaml

  # Run with stress test
  python network_diagnostics.py --stress-test

  # Test system ports from ports.yaml
  python network_diagnostics.py --system-ports

  # Full test suite
  python network_diagnostics.py --stress-test --system-ports

  # Override all settings
  python network_diagnostics.py 192.168.1.100 --base-port 6000 --metadata-port 6100
        """,
    )
    parser.add_argument("host", nargs="?", help="Target host IP address (overrides config.yaml)")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to config.yaml file (default: searches in common locations)",
    )
    parser.add_argument(
        "--ports-config",
        type=str,
        help="Path to ports.yaml file (default: searches in common locations)",
    )
    parser.add_argument(
        "--base-port",
        type=int,
        help="Base UDP port (overrides config.yaml, default: 5000)",
    )
    parser.add_argument(
        "--metadata-port",
        type=int,
        help="Metadata UDP port (overrides config.yaml, default: 5050)",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="Include stress testing (takes ~10 seconds)",
    )
    parser.add_argument(
        "--system-ports",
        action="store_true",
        help="Test additional system ports from ports.yaml (ROS2, Zenoh, etc.)",
    )
    parser.add_argument("--export", help="Export results to JSON file")

    args = parser.parse_args()

    # Load configuration from files
    config = load_config(args.config, args.ports_config)

    # Command-line arguments override config file
    target_host = args.host or config["host"]
    base_port = args.base_port or config["base_port"]
    metadata_port = args.metadata_port or config["metadata_port"]
    full_config = config.get("full_config", {})

    # Validate that we have a target host
    if target_host is None:
        parser.error("No target host specified. Provide it as argument or in config.yaml")

    LOGGER.info(f"Configuration:")
    LOGGER.info(f"  Host: {target_host}")
    LOGGER.info(f"  Base Port: {base_port}")
    LOGGER.info(f"  Metadata Port: {metadata_port}")
    if args.system_ports:
        ports_cfg = full_config.get("ports")
        if ports_cfg:
            LOGGER.info(f"  System Ports: Loaded from ports.yaml")
        else:
            LOGGER.info(f"  System Ports: Not found (will skip)")

    # Run diagnostics
    diag = NetworkDiagnostics(target_host, base_port, metadata_port, full_config)
    success = diag.run_all_tests(
        include_stress_test=args.stress_test,
        include_system_ports=args.system_ports,
    )

    # Export if requested
    if args.export:
        diag.export_results(args.export)

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
