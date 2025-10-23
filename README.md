# GStreamer RealSense Launch
This repository is NOT open source. The code is publicly viewable for reading
and evaluation only. Any use, build, modification, or distribution requires
a written license from Wistron. Contact: michael_hsu@wistron.com

## 📈 Project Status

![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![ROS 2](https://img.shields.io/badge/ROS%202-Humble-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Current Version**: 0.1.0 (Beta)

**Tested On**:
- Ubuntu 22.04 LTS
- ROS 2 Humble
- RealSense D435i


<p align="center">
  <strong>High-performance, low-latency RealSense camera streaming for ROS 2 using GStreamer.</strong>
</p>

<p align="center">
  <a href="#-overview">Overview</a> •
  <a href="#-features">Features</a> •
  <a href="#-requirements">Requirements</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-usage">Usage</a> •
  <a href="#-ros-2-integration">ROS 2 Integration</a> •
  <a href="#-configuration">Configuration</a>
</p>

---

## 📋 Overview

`gst_realsense_launch` is a ROS 2 package that enables efficient streaming of Intel RealSense camera data over network using GStreamer. It supports multiple camera streams (depth, color, infrared) with hardware-accelerated encoding and low-latency transmission.

### Key Components

- **gst_sender.py**: Auto-detects RealSense cameras and streams video over UDP/RTP
- **gst_receiver.py**: Receives RTP streams and publishes to ROS 2 topics or displays video
- **rs_camera_info_parser.py**: Parses RealSense intrinsics and generates camera_info YAML files
- **gscam_nvblox.launch.py**: ROS 2 launch file for integration with nvblox SLAM

---

## ✨ Features

- 🎥 **Multi-Camera Support**: Automatically detects and streams from multiple RealSense cameras
- 🚀 **Hardware Acceleration**: NVIDIA H.264 encoding (nvh264enc) support for color streams
- 📦 **Lossless Depth Streaming**: JPEG2000 encoding for 16-bit depth preservation
- 🌐 **Network Streaming**: UDP/RTP streaming with configurable ports
- 🤖 **ROS 2 Integration**: Direct publishing to ROS 2 topics via gscam
- 📐 **Camera Calibration**: Automatic camera_info generation from RealSense intrinsics
- 🔧 **Flexible Configuration**: YAML-based port and stream configuration


### d435i
/dev/video0 → Depth (Z16) ✓
/dev/video1 → Metadata (None) - pass
/dev/video2 → Infrared (GREY) ✓ - double IR (Y8I)
/dev/video3 → Metadata (None) - pass
/dev/video4 → Color (YUYV) ✓
/dev/video5 → Metadata (None) - pass

### Supported Streams

| Stream Type | Encoding | Default Port | Description |
|------------|----------|--------------|-------------|
| Color      | H.264    | 5000         | RGB video stream |
| Depth      | JPEG2000 | 5002         | 16-bit depth map |
| Infrared L | H.264    | 5004         | Left IR camera |
| Infrared R | H.264    | 5006         | Right IR camera |


### pt and payload
| Stream       | Encoding | RTP PT  |
| ------------ | -------- | ------- |
| depth        | H264     | **96**  |
| depth        | JPEG2000 | **112** |
| color        | H264     | **98**  |
| ir           | H264     | **97**  |
| infra_stereo | H264     | **99**  |


### Format for gst and ros2

| Stream | Format      | ROS Encoding | GStreamer Output               |
| ------ | ----------- | ------------ | ------------------------------ |
| Color  | `RGB`       | `rgb8`       | `video/x-raw,format=RGB`       |
| Depth  | `GRAY16_LE` | `16UC1`      | `video/x-raw,format=GRAY16_LE` |
| Infra  | `GRAY8`     | `mono8`      | `video/x-raw,format=GRAY8`     |


---

## 📦 Requirements

### System Requirements

- Ubuntu 22.04 (Jammy) or later
- ROS 2 Humble or later
- Python 3.10+
- Intel RealSense D435i camera

### Dependencies

**System packages** (install via `apt_install.sh`):
- GStreamer 1.0 with plugins (base, good, bad, ugly, libav)
- ROS 2 packages: `gscam`, `depth_image_proc`
- Intel RealSense SDK 2.0
- V4L2 utilities

---

## 🚀 Installation

### 1. Clone the Repository
```bash
cd ~/ros2_ws/src
git clone https://github.com/CTHMIT/realsense_gst_nvblox.git
cd gst_realsense_launch
```
### 2. Install System Dependencies
```bash
# Run the installation script
bash src/scripts/apt_install.sh

# Or install manually:
sudo apt update
sudo apt install -y \
  ros-humble-gscam \
  ros-humble-depth-image-proc \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  v4l-utils \
  librealsense2-dev \
  librealsense2-utils
```

## 🎯 Usage
### Quick Start: Streaming Camera
#### On the Camera Host (Sender)
```bash
# Auto-detect all cameras and stream at 424x240
pdm run src/gst_realsense_launch/gst_sender.py --host <receiver-ip> --size 424x240 --run

# Stream with preset for D435i camera
pdm run src/gst_realsense_launch/gst_sender.py --host 192.168.1.100 --preset d435i --run

# Stream specific device
pdm run src/gst_realsense_launch/gst_sender.py --host 192.168.1.100 --device /dev/video6 --run

# List detected cameras without streaming
pdm run src/gst_realsense_launch/gst_sender.py --list-only
```

#### On the Receiver (Display)
```bash
# Receive and display streams
pdm run receiver --ports 5000 5002 \
  --names "color" "depth" \
  --encodings h264 jpeg2000

# Receive with ROS 2 preset
pdm run receiver --preset d435i --base-port 5000 --ros2
```

## Advanced Usage
### Custom Port Configuration
#### Edit config/ports.yaml:
```yaml
udp:
  gstreamer:
    rtp_rtcp_pool:
      start: 5000
      end: 5099
    realsense_streams:
      color:
        rtp: 5000
        rtcp: 5001
      depth:
        rtp: 5002
        rtcp: 5003
```

## Project Structure
```
gst_realsense_launch/
├── config/                  # Camera calibration and configuration
│   ├── *_camera_*.yaml     # Camera info files
│   ├── ports.yaml          # Port configuration
│   └── gstreamer_cmd.txt   # Reference commands
├── gst_realsense_launch/   # Python package
│   └── __init__.py
├── launch/                  # Main scripts
│   ├── gst_sender.py       # Camera streaming sender
│   ├── gst_receiver.py     # Stream receiver
│   ├── rs_camera_info_parser.py  # Calibration parser
│   └── gscam_nvblox.launch.py    # ROS 2 launch file
├── pyproject.toml          # PDM configuration
├── package.xml             # ROS 2 package manifest
└── README.md
```

## 📊 Performance

Typical performance metrics on Intel i7 + NVIDIA GPU:

| Resolution | FPS | Encoding | Bitrate | Latency |
|-----------|-----|----------|---------|---------|
| 424x240   | 90  | H.264    | 2 Mbps  | <50ms   |
| 640x480   | 30  | H.264    | 4 Mbps  | <80ms   |
| 424x240   | 90  | JPEG2000 | 8 Mbps  | <100ms  |
| 640x480   | 30  | JPEG2000 | 15 Mbps | <150ms  |

### Performance Tips

- **Hardware Encoding**: Use `--encoder nvh264enc` for better performance
- **Resolution**: Lower resolutions (424x240) significantly reduce latency
- **Network**: Use gigabit Ethernet for stable streaming
- **CPU Usage**: ~15-25% per stream with hardware encoding

---


## 👤 Author

**Chun-Tse Hsu (cthsu)**

- 📧 Email: [chuntsehus@gmail.com](mailto:chuntsehus@gmail.com)
- 🐙 GitHub: [@C.T.Hsu](https://github.com/CTHMIT)
- 💼 Project: [realsense_gst_nvblox](https://github.com/CTHMIT/realsense_gst_nvblox)

---

## 🙏 Acknowledgments

This project wouldn't be possible without these amazing open-source projects:

- **[Intel RealSense](https://www.intelrealsense.com/)** - For the excellent depth camera SDK
- **[GStreamer](https://gstreamer.freedesktop.org/)** - For the powerful multimedia framework
- **[ROS 2](https://docs.ros.org/)** - For the robotics middleware
- **[gscam](https://github.com/ros-drivers/gscam)** - For the ROS-GStreamer bridge
- **[nvblox](https://nvidia-isaac-ros.github.io/concepts/scene_reconstruction/nvblox/)** - For 3D reconstruction integration

### Special Thanks

- The ROS 2 community for continuous support
- NVIDIA for CUDA and hardware acceleration tools
- All contributors who have helped improve this project

---

## 📖 References

### Documentation

- **[RealSense SDK Documentation](https://dev.intelrealsense.com/)** - Official Intel RealSense documentation
- **[GStreamer Documentation](https://gstreamer.freedesktop.org/documentation/)** - Complete GStreamer reference
- **[ROS 2 Humble Documentation](https://docs.ros.org/en/humble/)** - ROS 2 Humble tutorials and guides
- **[gscam Wiki](https://github.com/ros-drivers/gscam/wiki)** - gscam usage and examples

### Tutorials

- **[RealSense Getting Started](https://github.com/IntelRealSense/librealsense/blob/master/doc/readme.md)** - Camera setup guide
- **[GStreamer Tutorials](https://gstreamer.freedesktop.org/documentation/tutorials/index.html)** - Learn GStreamer basics
- **[ROS 2 Tutorials](https://docs.ros.org/en/humble/Tutorials.html)** - ROS 2 learning resources
- **[nvblox Tutorial](https://nvidia-isaac-ros.github.io/concepts/scene_reconstruction/nvblox/tutorials.html)** - 3D reconstruction setup

### Related Projects

- **[realsense-ros](https://github.com/IntelRealSense/realsense-ros)** - Official ROS wrapper for RealSense cameras
- **[isaac_ros_nvblox](https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_nvblox)** - NVIDIA 3D reconstruction package
- **[rtabmap_ros](https://github.com/introlab/rtabmap_ros)** - Real-time SLAM solution using RealSense

---

## 🌟 Show Your Support

If you find this project useful, please consider:

- ⭐ Starring the repository on GitHub
- 🐛 Reporting bugs and issues
- 💡 Suggesting new features
- 🔀 Contributing code improvements
- 📢 Sharing with the robotics community

---

<p align="center">
  <img src="https://img.shields.io/badge/Made%20with-❤️-red" alt="Made with love">
  <img src="https://img.shields.io/badge/For-Robotics%20Community-blue" alt="For robotics">
  <img src="https://img.shields.io/badge/Powered%20by-GStreamer-orange" alt="GStreamer">
  <img src="https://img.shields.io/badge/Built%20for-ROS%202-blueviolet" alt="ROS 2">
</p>

<p align="center">
  <strong>Made with ❤️ for the robotics community</strong>
</p>

<p align="center">
  <sub>© 2025 Chun-Tse Hsu. All rights reserved.</sub>
</p>
