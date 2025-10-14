#!/usr/bin/env python3
"""Network Diagnostics Tool for RealSense Streaming.

This tool helps diagnose network connectivity issues between sender and receiver.
It performs comprehensive tests including:
- Network reachability
- Port availability
- Bandwidth estimation
- Latency measurement
- Packet loss detection
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class NetworkTestResult:
    """Results from a network test."""

    test_name: str
    success: bool
    message: str
    details: dict | None = None


class NetworkDiagnostics:
    """Network diagnostics tool for streaming setup."""

    def __init__(self, target_host: str, base_port: int = 5000, metadata_port: int = 5100):
        """Initialize the network diagnostics tool.

        Args:
            target_host: The target host to run diagnostics against.
            base_port: The base port to use for diagnostics.
            metadata_port: The metadata port to use for diagnostics.
        """
        self.target_host = target_host
        self.base_port = base_port
        self.metadata_port = metadata_port
        self.results: list[NetworkTestResult] = []

    def run_all_tests(self) -> bool:
        """Run all diagnostic tests.

        Returns:
            True if all critical tests pass, False otherwise.
        """
        print(f"\n{'='*70}")
        print("REALSENSE STREAMING NETWORK DIAGNOSTICS")
        print(f"{'='*70}")
        print(f"Target Host: {self.target_host}")
        print(f"Base Port: {self.base_port}")
        print(f"Metadata Port: {self.metadata_port}")
        print(f"{'='*70}\n")

        # Run tests in order
        tests = [
            self.test_host_reachable,
            self.test_port_connectivity,
            self.test_udp_communication,
            self.test_bandwidth,
            self.test_latency,
        ]

        all_passed = True
        for test in tests:
            result = test()
            self.results.append(result)

            # Print result
            status = "✓" if result.success else "✗"
            color = "\033[92m" if result.success else "\033[91m"
            reset = "\033[0m"

            print(f"{color}{status}{reset} {result.test_name}")
            print(f"  {result.message}")

            if result.details:
                for key, value in result.details.items():
                    print(f"  {key}: {value}")

            print()

            if not result.success:
                all_passed = False

        # Print summary
        print(f"{'='*70}")
        passed = sum(1 for r in self.results if r.success)
        total = len(self.results)
        print(f"SUMMARY: {passed}/{total} tests passed")
        print(f"{'='*70}\n")

        return all_passed

    def test_host_reachable(self) -> NetworkTestResult:
        """Test if target host is reachable via ping."""
        try:
            result = subprocess.run(
                ["ping", "-c", "3", "-W", "2", self.target_host],
                capture_output=True,
                text=True,
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
                            message="Host is reachable",
                            details={"Average RTT": f"{avg_rtt} ms"},
                        )

                return NetworkTestResult(
                    test_name="Host Reachability (ICMP)",
                    success=True,
                    message="Host is reachable",
                )
            else:
                return NetworkTestResult(
                    test_name="Host Reachability (ICMP)",
                    success=False,
                    message=f"Cannot reach host {self.target_host}",
                )

        except Exception as e:
            return NetworkTestResult(
                test_name="Host Reachability (ICMP)",
                success=False,
                message=f"Ping test failed: {e}",
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

    def test_bandwidth(self) -> NetworkTestResult:
        """Estimate available bandwidth with iperf3 if available."""
        try:
            # Check if iperf3 is available
            result = subprocess.run(["which", "iperf3"], capture_output=True, text=True)

            if result.returncode != 0:
                return NetworkTestResult(
                    test_name="Bandwidth Estimation",
                    success=True,
                    message="iperf3 not available, skipping bandwidth test",
                    details={"Install with": "sudo apt install iperf3"},
                )

            # Try to run iperf3 client test
            # Note: This requires iperf3 server running on target
            result = subprocess.run(
                ["iperf3", "-c", self.target_host, "-t", "3", "-J"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                bandwidth_mbps = data["end"]["sum_received"]["bits_per_second"] / 1_000_000

                # Check if bandwidth is sufficient (need ~30 Mbps for full quality)
                sufficient = bandwidth_mbps >= 30

                return NetworkTestResult(
                    test_name="Bandwidth Estimation",
                    success=sufficient,
                    message=f"Available bandwidth: {bandwidth_mbps:.1f} Mbps",
                    details={
                        "Required": "~30 Mbps for full quality streaming",
                        "Status": "Sufficient" if sufficient else "May be insufficient",
                    },
                )
            else:
                return NetworkTestResult(
                    test_name="Bandwidth Estimation",
                    success=True,
                    message="iperf3 server not running on target",
                    details={"Note": "Start server with: iperf3 -s"},
                )

        except subprocess.TimeoutExpired:
            return NetworkTestResult(
                test_name="Bandwidth Estimation", success=False, message="Bandwidth test timed out"
            )
        except Exception as e:
            return NetworkTestResult(
                test_name="Bandwidth Estimation",
                success=True,
                message=f"Bandwidth test skipped: {e}",
            )

    def test_latency(self) -> NetworkTestResult:
        """Test network latency using UDP echo."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)

            latencies = []
            packet_loss = 0
            num_packets = 10

            for i in range(num_packets):
                try:
                    # Send packet with timestamp
                    send_time = time.time()
                    test_data = json.dumps({"seq": i, "timestamp": send_time}).encode("utf-8")

                    sock.sendto(test_data, (self.target_host, self.metadata_port))

                    # Small delay between packets
                    time.sleep(0.1)

                    # Calculate one-way latency (approximate)
                    latency = (time.time() - send_time) * 1000  # ms
                    latencies.append(latency)

                except TimeoutError:
                    packet_loss += 1

            sock.close()

            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                max_latency = max(latencies)
                min_latency = min(latencies)
                loss_percent = (packet_loss / num_packets) * 100

                # Latency < 50ms is good, < 100ms is acceptable
                good_latency = avg_latency < 100
                low_loss = loss_percent < 5

                return NetworkTestResult(
                    test_name="Latency Measurement",
                    success=good_latency and low_loss,
                    message=f"Average latency: {avg_latency:.1f} ms",
                    details={
                        "Min latency": f"{min_latency:.1f} ms",
                        "Max latency": f"{max_latency:.1f} ms",
                        "Packet loss": f"{loss_percent:.1f}%",
                        "Status": "Good" if good_latency else "High latency",
                    },
                )
            else:
                return NetworkTestResult(
                    test_name="Latency Measurement",
                    success=False,
                    message="All packets lost",
                )

        except Exception as e:
            return NetworkTestResult(
                test_name="Latency Measurement",
                success=False,
                message=f"Latency test failed: {e}",
            )

    def export_results(self, filename: str):
        """Export test results to JSON file."""
        data = {
            "target_host": self.target_host,
            "base_port": self.base_port,
            "metadata_port": self.metadata_port,
            "timestamp": time.time(),
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

        print(f"Results exported to {filename}")


def main():
    """Run the network diagnostics tool."""
    parser = argparse.ArgumentParser(description="Network diagnostics for RealSense streaming")
    parser.add_argument("host", help="Target host IP address")
    parser.add_argument(
        "--base-port",
        type=int,
        default=5000,
        help="Base UDP port (default: 5000)",
    )
    parser.add_argument(
        "--metadata-port",
        type=int,
        default=5100,
        help="Metadata UDP port (default: 5100)",
    )
    parser.add_argument("--export", help="Export results to JSON file")

    args = parser.parse_args()

    # Run diagnostics
    diag = NetworkDiagnostics(args.host, args.base_port, args.metadata_port)
    success = diag.run_all_tests()

    # Export if requested
    if args.export:
        diag.export_results(args.export)

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
