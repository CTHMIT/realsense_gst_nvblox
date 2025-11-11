from __future__ import annotations

import numpy as np

from utils.logger import LOGGER


class DepthMergeProcessor:
    """8-bit depth streams to 16-bit."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.high_byte_buffer: np.ndarray | None = None
        self.low_byte_buffer: np.ndarray | None = None
        self.last_high_time = 0.0
        self.last_low_time = 0.0

    def update_high_byte(self, data: np.ndarray, timestamp: float):
        """high-bit data."""
        self.high_byte_buffer = data
        self.last_high_time = timestamp

    def update_low_byte(self, data: np.ndarray, timestamp: float):
        """low-bit data."""
        self.low_byte_buffer = data
        self.low_time = timestamp

    def get_merged_depth(self) -> np.ndarray | None:
        """Get merged 16-bit depth data."""
        if self.high_byte_buffer is None or self.low_byte_buffer is None:
            return None

        time_diff = abs(self.last_high_time - self.last_low_time)
        if time_diff > 0.1:
            LOGGER.warning(f"Depth high/low byte time mismatch: {time_diff*1000:.1f}ms")

        # merge: depth = (high << 8) | low
        high_shifted = self.high_byte_buffer.astype(np.uint16) << 8
        depth_16bit = high_shifted | self.low_byte_buffer.astype(np.uint16)

        return depth_16bit
