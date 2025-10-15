## Depth(/dev/video0 )
### Sender
```bash
DEV=/dev/video0 WIDTH=424 HEIGHT=240 FPS=30 HOST=10.28.132.110 PORT=5000; \
v4l2-ctl -d "$DEV" --set-fmt-video=width=$WIDTH,height=$HEIGHT --set-parm=$FPS --stream-mmap --stream-to=- \
| gst-launch-1.0 -e fdsrc fd=0 do-timestamp=true \
  ! videoparse format=gray16-le width=$WIDTH height=$HEIGHT framerate=$FPS/1 \
  ! videoconvert dither=none ! video/x-raw,format=GRAY8 \
  ! videoconvert ! video/x-raw,format=I420 \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=3000 key-int-max=30 \
  ! h264parse config-interval=1 \
  ! rtph264pay pt=96 mtu=1200 config-interval=1 \
  ! udpsink host="$HOST" port=$PORT sync=false async=false

```
## IR（/dev/video2 → GRAY8@640×480@30）

### Sender（GRAY8 → RTP/H264）
``` bash
gst-launch-1.0 -e \
  v4l2src device=/dev/video2 do-timestamp=true \
  ! video/x-raw,format=GRAY8,width=640,height=480,framerate=30/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max=30 \
  ! h264parse config-interval=1 \
  ! rtph264pay pt=97 mtu=1200 config-interval=1 \
  ! udpsink host=RECV_IP port=5002 sync=false async=false
```

### Receiver
``` bash
gst-launch-1.0 -e \
  udpsrc port=5002 caps="application/x-rtp,media=video,encoding-name=H264,payload=97,clock-rate=90000" \
  ! rtpjitterbuffer latency=40 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```

## RGB (/dev/video4 → YUYV@640×480@30)
### Sender（YUY2 → RTP/H264）
```bash
gst-launch-1.0 -e \
  v4l2src device=/dev/video4 do-timestamp=true \
  ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 \
  ! videoconvert \
  ! x264enc tune=zerolatency speed-preset=ultrafast bitrate=4000 key-int-max=30 \
  ! h264parse config-interval=1 \
  ! rtph264pay pt=98 mtu=1200 config-interval=1 \
  ! udpsink host=<receiver_ip> port=5004 sync=false async=false
```
### Receiver
```bash
gst-launch-1.0 -e \
  udpsrc port=5004 caps="application/x-rtp,media=video,encoding-name=H264,payload=98,clock-rate=90000" \
  ! rtpjitterbuffer latency=40 \
  ! rtph264depay ! h264parse ! avdec_h264 \
  ! videoconvert ! autovideosink sync=false
```
