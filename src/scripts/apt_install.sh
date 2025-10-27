#!/bin/bash
set -e

echo "===== [1/4] Updating package repository ====="
sudo apt-get update
sudo apt update
echo "===== [2/4] Installing GStreamer Packages ====="

sudo apt-get install -y \
    libgirepository1.0-dev \
    libcairo2-dev \
    libxt-dev \
    libgirepository1.0-dev \
    python3-gi \
    gir1.2-gtk-3.0 \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    v4l-utils \
    iperf3 \
    tmux \
    ffmpeg \
    gstreamer1.0-vaapi \

echo "===== [3/4] Installing Intel RealSense SDK ====="
sudo mkdir -p /etc/apt/keyrings
curl -sSf https://librealsense.intel.com/Debian/librealsense.pgp | \
    sudo gpg --dearmor -o /etc/apt/keyrings/librealsense.gpg

echo "deb [signed-by=/etc/apt/keyrings/librealsense.gpg] https://librealsense.intel.com/Debian/apt-repo $(lsb_release -cs) main" | \
    sudo tee /etc/apt/sources.list.d/librealsense.list

sudo apt-get update
sudo apt-get install -y \
    librealsense2-dkms \
    librealsense2-utils \
    librealsense2-dev

echo "===== [4/4] Installing ROS2 Packages ====="
if [ -z "$ROS_DISTRO" ]; then
    ROS_DISTRO_TO_INSTALL="humble"
else
    ROS_DISTRO_TO_INSTALL=$ROS_DISTRO
fi

sudo apt-get install -y \
    ros-${ROS_DISTRO_TO_INSTALL}-gscam \
    ros-${ROS_DISTRO_TO_INSTALL}-depth-image-proc \
    ros-${ROS_DISTRO_TO_INSTALL}-cv-bridge

sudo apt update

echo "Installation complete!"
