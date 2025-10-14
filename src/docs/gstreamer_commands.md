Complete reference for V4L2, network ports, and system dependencies configuration.

---

## V4L2 Device Management

### Check Available Video Devices
```bash
# List all video devices
v4l2-ctl --list-devices

# Example output:
# Intel(R) RealSense(TM) Depth Camera 435i (usb-0000:00:14.0-1):
#         /dev/video0
#         /dev/video1
#         /dev/video2
#         /dev/video3
```
### Inspect Device Capabilities
```bash
# Check all device information
v4l2-ctl -d /dev/video2 --all

# Filter for name and format information
v4l2-ctl -d /dev/video2 --all | grep -E "Name|Format"

# Example output:
#         Name                 : Intel(R) RealSense(TM) Depth Ca
#         Type                 : Video Capture
#         Format Video Capture:
#                 Width/Height      : 640/480
```
### List Supported Formats
```bash
# List all supported formats and resolutions
v4l2-ctl --device=/dev/video6 --list-formats-ext

# Example output:
# ioctl: VIDIOC_ENUM_FMT
#         Type: Video Capture
#         [0]: 'YUYV' (YUYV 4:2:2)
#                 Size: Discrete 640x480
#                         Interval: Discrete 0.033s (30.000 fps)
#         [1]: 'MJPG' (Motion-JPEG, compressed)
#                 Size: Discrete 1920x1080
#                         Interval: Discrete 0.033s (30.000 fps)
```

## Network Port Configuration

### Port Allocation Overview

This project uses a structured port allocation scheme to avoid conflicts between different middleware and streaming protocols.

### UDP Ports

| Service | Port Range | Specific Ports | Description |
|---------|-----------|----------------|-------------|
| **ROS 2 / Fast DDS** | | | |
| Domain 0 (Multicast) | 7400-7401 | 7400, 7401 | Discovery multicast |
| Domain 0 (Unicast) | 7410-7649 | - | Participant communication |
| Domain 161 (Multicast) | 47650-47651 | 47650, 47651 | Alternative domain discovery |
| Domain 161 (Unicast) | 47660-47899 | - | Alternative domain communication |
| **Fast DDS Discovery** | 11811 | 11811 | Discovery Server |
| **Zenoh** | | | |
| Scouting | 7446 | 7446 | Peer discovery |
| QUIC Transport | 7447 | 7447 | Data transport |
| **GStreamer RTP/RTCP** | 5000-5099 | - | Streaming pool |
| Color Stream | 5000-5001 | 5000 (RTP), 5001 (RTCP) | RGB camera |
| Depth Stream | 5002-5003 | 5002 (RTP), 5003 (RTCP) | Depth camera |
| Infrared Left | 5004-5005 | 5004 (RTP), 5005 (RTCP) | IR camera left |
| Infrared Right | 5006-5007 | 5006 (RTP), 5007 (RTCP) | IR camera right |

### TCP Ports

| Service | Port Range | Specific Ports | Description |
|---------|-----------|----------------|-------------|
| **Fast DDS Discovery** | 42100 | 42100 | Discovery Server TCP |
| **ROS 2 / Fast DDS** | | | |
| Domain 0 | 7410-7649 | - | TCP fallback |
| Domain 161 | 47660-47899 | - | TCP fallback |
| **Zenoh** | | | |
| Router/Client | 7445-7447 | 7445, 7446, 7447 | Router connections |
| REST API | 8000 | 8000 | HTTP REST interface |
| **GStreamer RTSP** | 8554 | 8554 | RTSP server |
