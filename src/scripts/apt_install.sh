#!/bin/bash

# =================================================================================
# Dependency Installation Script for the gst_realsense_launch Project
#
# This script installs all required system-level dependencies, including:
# 1. Core system utilities and GStreamer plugins.
# 2. The Intel RealSense SDK (librealsense).
# 3. ROS2 packages for GStreamer integration (gscam).
#
# NOTE: Python packages are managed by PDM and are NOT installed by this script.
#
# Usage:
# 1. Grant execute permissions: chmod +x setup_dependencies.sh
# 2. Run the script:         ./setup_dependencies.sh
# =================================================================================

# Exit immediately if a command exits with a non-zero status.
set -e

echo "===== [1/4] Updating package repository information ====="
sudo apt-get update

echo "===== [2/4] Installing System Utilities & GStreamer Packages ====="
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    v4l-utils \
    iperf3 \
    tmux \

echo "===== [3/4] Installing Intel RealSense SDK (librealsense) ====="
# Set up the Intel RealSense Debian repository as per official instructions
sudo mkdir -p /etc/apt/keyrings
curl -sSL https://librealsense.intel.com/Debian/librealsense.co/pubkey.gpg | sudo gpg --dearmor -o /etc/apt/keyrings/librealsense.gpg

echo "deb [signed-by=/etc/apt/keyrings/librealsense.gpg] https://librealsense.intel.com/Debian/librealsense.co $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/librealsense.list > /dev/null

sudo apt-get update

# Install the core SDK libraries and developer tools
sudo apt-get install -y \
    librealsense2-dkms \
    librealsense2-utils \
    librealsense2-dev

echo "===== [4/4] Installing ROS2 GStreamer Packages (gscam) ====="
# This package provides a GStreamer-based ROS camera driver.
# It attempts to auto-detect your ROS2 distribution.
if [ -z "$ROS_DISTRO" ]; then
    echo "Warning: ROS_DISTRO environment variable not set. Assuming 'humble' for gscam installation."
    ROS_DISTRO_TO_INSTALL="humble"
else
    echo "Detected ROS2 distribution: $ROS_DISTRO"
    ROS_DISTRO_TO_INSTALL=$ROS_DISTRO
fi
sudo apt-get install -y ros-${ROS_DISTRO_TO_INSTALL}-gscam

echo ""
echo "========================================================"
echo "System-level dependencies installed successfully!"
echo ""
echo "Next Steps:"
echo "1. Source your ROS2 environment if you haven't already:"
echo "   source /opt/ros/$ROS_DISTRO_TO_INSTALL/setup.bash"
echo "2. Use PDM to install the required Python packages:"
echo "   pdm install"
echo "========================================================"
