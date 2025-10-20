## Manual GStreamer Commands

### Depth (/dev/video0) - Using H.264

#### Sender
```bash
DEV=/dev/video0
WIDTH=640
HEIGHT=480
FPS=30
HOST=10.28.132.110
PORT=5000
BITRATE=8000
BUFFER=2000000

v4l2-ctl -d "$DEV" \
    --set-fmt-video=width=$WIDTH,height=$HEIGHT,pixelformat='Z16 ' \
    --set-parm=$FPS \
    --stream-mmap \
    --stream-to=- | \
gst-launch-1.0 -e -v fdsrc fd=0 \
    ! videoparse format=gray16-le width=$WIDTH height=$HEIGHT framerate=$FPS/1 \
    ! queue max-size-buffers=2 leaky=downstream \
    ! videoconvert \
    ! video/x-raw,format=I420 \
    ! nvh264enc preset=hq rc-mode=cbr-ld-hq bitrate=$BITRATE gop-size=$FPS zerolatency=true qp-min=10 qp-max=25 \
    ! video/x-h264,profile=high \
    ! h264parse config-interval=1 \
    ! rtph264pay pt=96 mtu=1400 \
    ! udpsink host="$HOST" port=$PORT sync=false async=false buffer-size=$BUFFER

```

#### Receiver
```bash
# Parameters from config.yaml
BUFFER_SIZE=2097152  # streaming.udp.buffer_size
LATENCY=100          # streaming.jitter_buffer.latency
MAX_THREADS=4        # streaming.processing.max_threads
N_THREADS=4          # streaming.processing.n_threads

gst-launch-1.0 -v \
  udpsrc port=5000 buffer-size=$BUFFER_SIZE ! \
  application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96 ! \
  rtpjitterbuffer latency=$LATENCY drop-on-latency=true ! \
  rtph264depay ! \
  h264parse ! \
  avdec_h264 max-threads=$MAX_THREADS skip-frame=0 ! \
  queue max-size-buffers=2 leaky=downstream ! \
  videoconvert n-threads=$N_THREADS ! \
  autovideosink sync=false
```

---

### IR Left (/dev/video2) - GRAY8

#### Sender
```bash
DEV=/dev/video2
WIDTH=640
HEIGHT=480
FPS=30
HOST=10.28.132.110
PORT=5004  # infra1 port from config
BITRATE=2000

gst-launch-1.0 -e \
  v4l2src device=$DEV do-timestamp=true \
  ! video/x-raw,format=GRAY8,width=$WIDTH,height=$HEIGHT,framerate=$FPS/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=$BITRATE key-int-max=$FPS \
  ! h264parse config-interval=1 \
  ! rtph264pay pt=97 \
  ! udpsink host=$HOST port=$PORT sync=false async=false
```

#### Receiver
```bash
gst-launch-1.0 -e \
  udpsrc port=5004 caps="application/x-rtp,media=video,encoding-name=H264,payload=97" \
  ! rtpjitterbuffer latency=50 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```

---

### RGB (/dev/video4) - YUYV/YUY2

#### Sender
```bash
DEV=/dev/video4
WIDTH=640
HEIGHT=480
FPS=30
HOST=10.28.132.110
PORT=5000  # color port from config
BITRATE=8000  # encoding.h264.bitrate from config

gst-launch-1.0 -e \
  v4l2src device=$DEV do-timestamp=true \
  ! video/x-raw,format=YUY2,width=$WIDTH,height=$HEIGHT,framerate=$FPS/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=$BITRATE key-int-max=$FPS \
  ! h264parse config-interval=1 \
  ! rtph264pay pt=98 \
  ! udpsink host=$HOST port=$PORT sync=false async=false
```

#### Receiver
```bash
gst-launch-1.0 -e \
  udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=98" \
  ! rtpjitterbuffer latency=50 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```

---

## 🎛️ Parameter Tuning Guide

### Improve Quality
Modify in `config.yaml`:
```yaml
encoding:
  h264:
    bitrate: 12000        # Increase to 12 Mbps
    speed_preset: "fast"  # Change to fast (higher quality)
```

### Reduce Latency
```yaml
streaming:
  jitter_buffer:
    latency: 50           # Reduce to 50ms

  queue:
    max_size_buffers: 1   # Reduce buffering
```

### For Unstable Networks
```yaml
streaming:
  udp:
    buffer_size: 4194304  # Increase to 4MB

  jitter_buffer:
    latency: 200          # Increase to 200ms
    drop_on_latency: false
```

---

## 🔍 Troubleshooting

### Stuttering Video
1. Increase `streaming.udp.buffer_size`
2. Increase `streaming.jitter_buffer.latency`
3. Decrease `encoding.h264.bitrate`

### High Latency
1. Decrease `streaming.jitter_buffer.latency`
2. Set `streaming.queue.max_size_buffers` = 1
3. Use `speed_preset: "ultrafast"`

### Poor Quality
1. Increase `encoding.h264.bitrate`
2. Change to `speed_preset: "medium"` or `"fast"`
3. For depth images, set `depth_h264.use_h264: false` to use JPEG2000

---

## 💡 Best Practices

1. **Wired LAN**: Use high bitrate (10000-15000) + fast preset
2. **WiFi Environment**: Use medium bitrate (6000-8000) + ultrafast preset
3. **Accurate Depth**: Set `depth_h264.use_h264: false` to use JPEG2000
4. **Real-time Control**: latency=20-50, use ultrafast preset
5. **Recording/Analysis**: latency=100-200, use medium preset, high bitrate

---

## 📝 Quick Reference

### Port Mapping (from config.yaml)
| Stream | RTP Port | RTCP Port |
|--------|----------|-----------|
| Color  | 5000     | 5001      |
| Depth  | 5002     | 5003      |
| IR1    | 5004     | 5005      |
| IR2    | 5006     | 5007      |
| IMU    | 5050     | -         |

### Common Resolutions
| Resolution | Usage |
|------------|-------|
| 640x480    | Standard, balanced |
| 848x480    | Wide view |
| 1280x720   | High quality |
| 424x240    | Low bandwidth |

### Bitrate Recommendations
| Quality | Bitrate (kbps) |
|---------|----------------|
| Low     | 3000-5000      |
| Medium  | 6000-8000      |
| High    | 10000-15000    |

---

## 🚀 Advanced Examples

### High Quality Recording
```bash
# Sender with high bitrate
pdm run src/gst_realsense_launch/gst_sender.py \
  --preset d435i \
  --bitrate 15000 \
  --encoder x264enc \
  --run
```

### Low Latency Streaming
```bash
# Modify config.yaml first:
# streaming.jitter_buffer.latency: 30
# streaming.queue.max_size_buffers: 1

pdm run src/gst_realsense_launch/gst_sender.py --preset d435i --run
```

### Multiple Cameras
```bash
# List available cameras
pdm run src/gst_realsense_launch/gst_sender.py --list-only

# Stream specific camera
pdm run src/gst_realsense_launch/gst_sender.py \
  --device /dev/video0 \
  --run
```

---

## 🔗 Related Documentation

- `config.yaml` - Complete configuration file
- `README.md` - Project overview
- `使用指南.md` - Detailed usage guide (Chinese)

---

## ❓ FAQ

**Q: How to change resolution?**

A: Modify `config.yaml`:
```yaml
camera:
  resolution: "848x480"  # or "640x480", "1280x720"
```

**Q: How to stream multiple cameras simultaneously?**

A: The Python script automatically detects and streams all cameras

**Q: How to stream only depth images?**

A: Use manual commands, refer to the Depth section above

**Q: Should I use JPEG2000 or H.264 for depth?**

A:
- JPEG2000: Preserves 16-bit precision, suitable for accurate depth applications
- H.264: Lower bandwidth, smoother, suitable for general vision applications

**Q: How to verify configuration is loaded?**

A: Check the output when running:
```
✓ Loaded configuration from src/config/config.yaml
```

---

## 📊 Performance Monitoring

### Check FPS
Sender will display:
```
<<< 29.99 fps
```

### Check Network
```bash
# Check packet loss
netstat -su | grep "packet receive errors"

# Monitor traffic
sudo tcpdump -i any port 5002 -c 100
```

### Check CPU Usage
```bash
top -p $(pgrep gst-launch)
```

---

## 🛠️ System Optimization

### Increase System UDP Buffers (Important!)
```bash
# Temporary (on receiver)
sudo sysctl -w net.core.rmem_max=26214400
sudo sysctl -w net.core.rmem_default=26214400

# Permanent
echo "net.core.rmem_max=26214400" | sudo tee -a /etc/sysctl.conf
echo "net.core.rmem_default=26214400" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### Firewall Configuration
```bash
sudo ufw allow 5000:5010/udp
sudo ufw allow 5050/udp
```

### Performance Mode
```bash
# Set CPU to performance mode
sudo cpupower frequency-set -g performance
```
