#!/usr/bin/env bash
set -euo pipefail

DEV=/dev/video0
W=640; H=480; FPS=30
DST=10.28.132.201
PORT_H=5021     # pt=114
PORT_L=5022     # pt=115
HPIPE=/tmp/depth_H8.y8
LPIPE=/tmp/depth_L8.y8

# 乾淨重建 FIFO
rm -f "$HPIPE" "$LPIPE"
mkfifo "$HPIPE" "$LPIPE"

# 確保結束時清掉 FIFO
cleanup(){ rm -f "$HPIPE" "$LPIPE"; }
trap cleanup EXIT

# 送高 8 位
gst-launch-1.0 -e -v \
  filesrc location="$HPIPE" do-timestamp=true ! \
  videoparse format=gray8 width=$W height=$H framerate=$FPS/1 ! \
  queue max-size-buffers=2 leaky=downstream ! \
  videoconvert ! video/x-raw,format=I420 ! \
  x264enc tune=zerolatency speed-preset=ultrafast bitrate=8000 key-int-max=$FPS ! \
  h264parse config-interval=1 ! rtph264pay pt=114 ! \
  udpsink host=$DST port=$PORT_H sync=false async=false &
PID_H=$!

# 送低 8 位
gst-launch-1.0 -e -v \
  filesrc location="$LPIPE" do-timestamp=true ! \
  videoparse format=gray8 width=$W height=$H framerate=$FPS/1 ! \
  queue max-size-buffers=2 leaky=downstream ! \
  videoconvert ! video/x-raw,format=I420 ! \
  x264enc tune=zerolatency speed-preset=ultrafast bitrate=8000 key-int-max=$FPS ! \
  h264parse config-interval=1 ! rtph264pay pt=115 ! \
  udpsink host=$DST port=$PORT_L sync=false async=false &
PID_L=$!

# 一次取相機 → ffmpeg 分兩路（高/低 8 位）→ 寫入兩個 FIFO
v4l2-ctl -d "$DEV" \
  --set-fmt-video=width=$W,height=$H,pixelformat='Z16 ' \
  --set-parm=$FPS --stream-mmap --stream-to=- 2>/dev/null |
ffmpeg -hide_banner -loglevel error \
  -f rawvideo -pix_fmt gray16le -s ${W}x${H} -r $FPS -i - \
  -filter_complex "[0:v]split=2[h][l]; \
                   [h]lut='val/256',format=gray[h8]; \
                   [l]lut='mod(val,256)',format=gray[l8]" \
  -map "[h8]" -f rawvideo -pix_fmt gray "$HPIPE" \
  -map "[l8]" -f rawvideo -pix_fmt gray "$LPIPE"

# 若前面中斷，可順手收掉兩條 gst
kill $PID_H $PID_L 2>/dev/null || true
