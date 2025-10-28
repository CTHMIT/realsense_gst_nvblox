#!/usr/bin/env python3
"""RealSense Tmux Session Manager

This module provides unified tmux session management for both sender and receiver.
"""

import subprocess
import time
from typing import Optional

from utils.logger import LOGGER


class TmuxSessionManager:
    """Unified tmux session manager for RealSense streaming.

    This class manages tmux sessions for both sender and receiver applications,
    providing a consistent interface for creating windows and running commands.
    """

    def __init__(self, session_name: str = "realsense", mode: str = "sender"):
        """Initialize tmux session manager.

        Args:
            session_name: Base name for the tmux session
            mode: Either "sender" or "receiver" to differentiate sessions
        """
        self.base_session_name = session_name
        self.mode = mode
        self.session_name = f"{session_name}_{mode}"
        self.window_count = 0
        self._check_tmux()
        self._cleanup_old_session()
        self._create_session()

    def _check_tmux(self):
        """Check if tmux is available on the system."""
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True, timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            raise RuntimeError("tmux is not installed. Please install tmux: sudo apt install tmux")

    def _session_exists(self) -> bool:
        """Check if the tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name], capture_output=True, timeout=5
        )
        return result.returncode == 0

    def _cleanup_old_session(self):
        """Kill old session if it exists."""
        if self._session_exists():
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", self.session_name],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                LOGGER.info(f"✓ Cleaned up old tmux session '{self.session_name}'")
                time.sleep(0.5)
            except subprocess.TimeoutExpired:
                LOGGER.warning(f"Timeout while cleaning up session '{self.session_name}'")

    def _create_session(self):
        """Create new tmux session."""
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name],
                check=True,
                capture_output=True,
                timeout=5,
            )
            LOGGER.info(f"✓ Created tmux session '{self.session_name}'")
        except subprocess.CalledProcessError as e:
            LOGGER.error(f"Failed to create tmux session: {e}")
            raise
        except subprocess.TimeoutExpired:
            LOGGER.error("Timeout while creating tmux session")
            raise

    def create_window(self, window_name: str, command: str) -> bool:
        """Create a new tmux window and run command in it.

        Args:
            window_name: Name for the new window
            command: Shell command to execute in the window

        Returns:
            True if successful, False otherwise
        """
        try:
            # Create the window
            subprocess.run(
                ["tmux", "new-window", "-t", self.session_name, "-n", window_name],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Send command to the window
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{self.session_name}:{window_name}", command, "C-m"],
                check=True,
                capture_output=True,
                timeout=5,
            )

            self.window_count += 1
            LOGGER.info(f"  ✓ Created window '{window_name}' in tmux")
            return True

        except subprocess.CalledProcessError as e:
            LOGGER.error(f"  ✗ Failed to create window '{window_name}': {e}")
            if e.stderr:
                LOGGER.error(f"     Error output: {e.stderr}")
            return False
        except subprocess.TimeoutExpired:
            LOGGER.error(f"  ✗ Timeout creating window '{window_name}'")
            return False

    def send_keys(self, window_name: str, keys: str) -> bool:
        """Send keys to a specific window.

        Args:
            window_name: Target window name
            keys: Keys to send

        Returns:
            True if successful, False otherwise
        """
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{self.session_name}:{window_name}", keys, "C-m"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            LOGGER.error(f"Failed to send keys to window '{window_name}': {e}")
            return False

    def attach_info(self):
        """Display instructions for attaching to the tmux session."""
        LOGGER.info("=" * 60)
        LOGGER.info(f"To view the {self.mode} streams, attach to tmux session:")
        LOGGER.info(f"  tmux attach -t {self.session_name}")
        LOGGER.info("")
        LOGGER.info("Tmux navigation commands:")
        LOGGER.info("  Ctrl+b n         : next window")
        LOGGER.info("  Ctrl+b p         : previous window")
        LOGGER.info("  Ctrl+b [0-9]     : select window by number")
        LOGGER.info("  Ctrl+b w         : list all windows")
        LOGGER.info("  Ctrl+b d         : detach from session")
        LOGGER.info("  Ctrl+b &         : kill current window")
        LOGGER.info("=" * 60)

    def list_windows(self) -> list[str]:
        """List all windows in the session.

        Returns:
            List of window names
        """
        try:
            result = subprocess.run(
                ["tmux", "list-windows", "-t", self.session_name, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return [w.strip() for w in result.stdout.split("\n") if w.strip()]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

    def get_window_pids(self) -> list[str]:
        """Get all process IDs in the session.

        Returns:
            List of PIDs
        """
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", self.session_name, "-a", "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return [pid.strip() for pid in result.stdout.split("\n") if pid.strip()]
        except subprocess.TimeoutExpired:
            LOGGER.warning("Timeout while getting window PIDs")
        return []

    def kill_session(self, force: bool = False):
        """Kill the entire tmux session and all its processes.

        Args:
            force: If True, use SIGKILL after SIGTERM timeout
        """
        if not self._session_exists():
            LOGGER.info(f"Tmux session '{self.session_name}' already terminated")
            return

        LOGGER.info(f"Stopping tmux session '{self.session_name}'...")

        try:
            # Get all PIDs in the session
            pids = self.get_window_pids()

            if pids:
                LOGGER.info(f"Found {len(pids)} processes in tmux session")

                # Send SIGTERM to all processes
                for pid in pids:
                    if pid.isdigit():
                        try:
                            subprocess.run(
                                ["kill", "-TERM", pid], timeout=2, check=False, capture_output=True
                            )
                            LOGGER.debug(f"  Sent SIGTERM to PID {pid}")
                        except subprocess.TimeoutExpired:
                            LOGGER.debug(f"  Timeout sending SIGTERM to PID {pid}")

                # Wait for graceful shutdown
                time.sleep(2)

                # Force kill if requested
                if force:
                    for pid in pids:
                        if pid.isdigit():
                            try:
                                # Check if process still exists
                                check = subprocess.run(
                                    ["ps", "-p", pid], capture_output=True, timeout=1, check=False
                                )
                                if check.returncode == 0:
                                    subprocess.run(
                                        ["kill", "-KILL", pid],
                                        timeout=1,
                                        check=False,
                                        capture_output=True,
                                    )
                                    LOGGER.debug(f"  Force killed PID {pid}")
                            except subprocess.TimeoutExpired:
                                LOGGER.debug(f"  Timeout force killing PID {pid}")

            # Kill the tmux session itself
            result = subprocess.run(
                ["tmux", "kill-session", "-t", self.session_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            if result.returncode == 0:
                LOGGER.info(f"✓ Killed tmux session '{self.session_name}'")
            else:
                LOGGER.warning(f"Failed to kill session (may already be dead): {result.stderr}")

            # Verify cleanup
            time.sleep(0.5)
            if not self._session_exists():
                LOGGER.info("✓ Tmux session cleanup verified")
            else:
                LOGGER.warning("⚠ Tmux session may still exist")

        except subprocess.TimeoutExpired:
            LOGGER.error("Timeout during tmux cleanup")
        except Exception as e:
            LOGGER.error(f"Error during tmux cleanup: {e}")

    def kill_window(self, window_name: str) -> bool:
        """Kill a specific window.

        Args:
            window_name: Name of window to kill

        Returns:
            True if successful, False otherwise
        """
        try:
            subprocess.run(
                ["tmux", "kill-window", "-t", f"{self.session_name}:{window_name}"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            LOGGER.info(f"✓ Killed window '{window_name}'")
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            LOGGER.error(f"Failed to kill window '{window_name}': {e}")
            return False

    def is_window_running(self, window_name: str) -> bool:
        """Check if a specific window is running.

        Args:
            window_name: Window name to check

        Returns:
            True if window exists, False otherwise
        """
        windows = self.list_windows()
        return window_name in windows

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup session."""
        self.kill_session(force=True)
