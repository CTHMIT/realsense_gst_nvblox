# RealSense Streaming Commands

## 🎯 配置說明

所有參數現在都可以在 `config.yaml` 中修改：

### 關鍵配置參數
```yaml
network:
  server_ip: "10.28.132.110"  # 修改為你的接收端 IP

encoding:
  h264:
    bitrate: 8000           # H.264 位元率 (kbps)
    tune: "zerolatency"     # 低延遲調整
    speed_preset: "ultrafast"  # 編碼速度

  depth_h264:
    bitrate: 8000
    use_h264: true          # true=H.264, false=JPEG2000

streaming:
  udp:
    buffer_size: 2097152    # UDP 緩衝大小

  jitter_buffer:
    latency: 100            # 抖動緩衝延遲 (ms)
    drop_on_latency: true

  processing:
    max_threads: 4          # 解碼執行緒數
    n_threads: 4            # 轉換執行緒數
```

---

## 📡 使用 Python 腳本 (推薦)

### 發送端
```bash
# 使用預設配置
pdm run src/gst_realsense_launch/gst_sender.py --preset d435i --run

# 覆蓋特定參數
pdm run src/gst_realsense_launch/gst_sender.py \
  --preset d435i \
  --host 10.28.132.110 \
  --resolution 640x480 \
  --fps 30 \
  --bitrate 8000 \
  --run
```

### 接收端
```bash
# 使用預設配置
pdm run receiver --preset d435i --show-views

# 覆蓋特定參數
pdm run receiver \
  --preset d435i \
  --base-port 5000 \
  --show-views \
  --view-scale 0.5
```

---

## 🔧 手動 GStreamer 命令

### Depth (/dev/video0) - 使用 H.264

#### 發送端
```bash
DEV=/dev/video0
WIDTH=640
HEIGHT=480
FPS=30
HOST=10.28.132.110
PORT=5000
BITRATE=8000  # 從 config.yaml: encoding.depth_h264.bitrate

v4l2-ctl -d "$DEV" \
    --set-fmt-video=width=$WIDTH,height=$HEIGHT,pixelformat='Z16 ' \
    --set-parm=$FPS \
    --stream-mmap \
    --stream-to=- | \
gst-launch-1.0 -e -v fdsrc fd=0 \
    ! videoparse format=gray16-le width=$WIDTH height=$HEIGHT framerate=$FPS/1 \
    ! videoconvert \
    ! video/x-raw,format=I420 \
    ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=$BITRATE key-int-max=$FPS \
    ! h264parse config-interval=1 \
    ! rtph264pay pt=96 \
    ! udpsink host="$HOST" port=$PORT sync=false async=false
```

#### 接收端
```bash
# 參數從 config.yaml 讀取
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

#### 發送端
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

#### 接收端
```bash
gst-launch-1.0 -e \
  udpsrc port=5004 caps="application/x-rtp,media=video,encoding-name=H264,payload=97" \
  ! rtpjitterbuffer latency=50 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```

---

### RGB (/dev/video4) - YUYV/YUY2

#### 發送端
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

#### 接收端
```bash
gst-launch-1.0 -e \
  udpsrc port=5000 caps="application/x-rtp,media=video,encoding-name=H264,payload=98" \
  ! rtpjitterbuffer latency=50 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```

---

## 🎛️ 調整參數指南

### 提高畫質
在 `config.yaml` 中修改：
```yaml
encoding:
  h264:
    bitrate: 12000        # 增加到 12 Mbps
    speed_preset: "fast"  # 改為 fast (更高品質)
```

### 降低延遲
```yaml
streaming:
  jitter_buffer:
    latency: 50           # 降低到 50ms

  queue:
    max_size_buffers: 1   # 減少緩衝
```

### 網路不穩定時
```yaml
streaming:
  udp:
    buffer_size: 4194304  # 增加到 4MB

  jitter_buffer:
    latency: 200          # 增加到 200ms
    drop_on_latency: false
```

---

## 🔍 故障排除

### 畫面卡頓
1. 增加 `streaming.udp.buffer_size`
2. 調高 `streaming.jitter_buffer.latency`
3. 降低 `encoding.h264.bitrate`

### 延遲太高
1. 降低 `streaming.jitter_buffer.latency`
2. 設定 `streaming.queue.max_size_buffers` = 1
3. 使用 `speed_preset: "ultrafast"`

### 畫質不佳
1. 提高 `encoding.h264.bitrate`
2. 改用 `speed_preset: "medium"` 或 `"fast"`
3. 對深度影像，設定 `depth_h264.use_h264: false` 使用 JPEG2000

---

## 💡 最佳實踐

1. **區域網路**: 使用高 bitrate (10000-15000) + fast preset
2. **WiFi 環境**: 使用中 bitrate (6000-8000) + ultrafast preset
3. **精確深度**: 設定 `depth_h264.use_h264: false` 使用 JPEG2000
4. **即時控制**: latency=20-50, 使用 ultrafast preset
5. **錄製分析**: latency=100-200, 使用 medium preset, 高 bitrate
