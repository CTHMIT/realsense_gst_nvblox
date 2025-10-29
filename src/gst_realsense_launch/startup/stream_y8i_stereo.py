#!/usr/bin/env python3
"""
RealSense D435i Y8I 雙路紅外線串流（可配置解析度版本）
"""
import signal
import subprocess
import sys

import cv2
import numpy as np

# ==================== 配置區 ====================
# 修改這裡來改變解析度和幀率
WIDTH = 640  # 單個紅外線的寬度（Y8I 實際寬度會是 2 倍）
HEIGHT = 460  # 高度
FPS = 30  # 幀率
BITRATE = 4000  # 編碼比特率 (kbps)
TARGET_HOST = "10.28.121.28"  # 目標 IP

# 常用解析度預設（取消註釋使用）
# WIDTH, HEIGHT, FPS = 424, 240, 30   # 低解析度，高幀率
# WIDTH, HEIGHT, FPS = 640, 480, 30   # 中解析度
# WIDTH, HEIGHT, FPS = 848, 480, 30   # 高解析度
# WIDTH, HEIGHT, FPS = 1280, 720, 30  # Full HD（最高品質）
# ================================================

Y8I_WIDTH = WIDTH * 2  # Y8I 格式寬度是 2 倍（左右交織）
TOTAL_PIXELS = HEIGHT * Y8I_WIDTH


def signal_handler(sig, frame):
    print("\n\n正在停止...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)

# 允許命令列參數覆蓋
if len(sys.argv) > 1:
    TARGET_HOST = sys.argv[1]
if len(sys.argv) > 2:
    WIDTH = int(sys.argv[2])
    Y8I_WIDTH = WIDTH * 2
if len(sys.argv) > 3:
    HEIGHT = int(sys.argv[3])
    TOTAL_PIXELS = HEIGHT * Y8I_WIDTH
if len(sys.argv) > 4:
    FPS = int(sys.argv[4])

print("=" * 60)
print("RealSense D435i Y8I 雙路紅外線串流")
print("=" * 60)
print(f"解析度:   {WIDTH}x{HEIGHT} @ {FPS} fps (每個紅外線)")
print(f"Y8I 寬度: {Y8I_WIDTH} (左右交織)")
print(f"目標主機: {TARGET_HOST}")
print(f"左紅外:   port 5031 (PT=96)")
print(f"右紅外:   port 5032 (PT=97)")
print(f"比特率:   {BITRATE} kbps")
print("=" * 60)

# 開啟相機
cap = cv2.VideoCapture("/dev/video2", cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc("Y", "8", "I", " "))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, Y8I_WIDTH)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
cap.set(cv2.CAP_PROP_FPS, FPS)

if not cap.isOpened():
    print("錯誤：無法開啟相機 /dev/video2")
    sys.exit(1)

# 驗證設置
actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
actual_fps = cap.get(cv2.CAP_PROP_FPS)

print(f"\n實際設置: {actual_width}x{actual_height} @ {actual_fps:.1f} fps")

if actual_width != Y8I_WIDTH or actual_height != HEIGHT:
    print(f"警告：實際解析度與請求不同！")
    print(f"請求: {Y8I_WIDTH}x{HEIGHT}, 實際: {actual_width}x{actual_height}")
    response = input("是否繼續？(y/N): ")
    if response.lower() != "y":
        sys.exit(1)
    # 更新參數
    Y8I_WIDTH = actual_width
    WIDTH = actual_width // 2
    HEIGHT = actual_height
    TOTAL_PIXELS = HEIGHT * Y8I_WIDTH

# 讀取測試幀
ret, test_frame = cap.read()
if not ret:
    print("錯誤：無法讀取相機畫面")
    sys.exit(1)

print(f"讀取幀: shape={test_frame.shape}, size={test_frame.size}, dtype={test_frame.dtype}")

# 創建 GStreamer pipelines
try:
    gst_left = subprocess.Popen(
        [
            "gst-launch-1.0",
            "-q",
            "fdsrc",
            "!",
            "videoparse",
            f"width={WIDTH}",
            f"height={HEIGHT}",
            f"framerate={FPS}/1",
            "format=2",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=I420",
            "!",
            "x264enc",
            "tune=zerolatency",
            "speed-preset=ultrafast",
            f"bitrate={BITRATE}",
            f"key-int-max={FPS}",
            "!",
            "h264parse",
            "config-interval=1",
            "!",
            "rtph264pay",
            "pt=96",
            "mtu=1400",
            "!",
            "udpsink",
            f"host={TARGET_HOST}",
            "port=5031",
            "sync=false",
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    gst_right = subprocess.Popen(
        [
            "gst-launch-1.0",
            "-q",
            "fdsrc",
            "!",
            "videoparse",
            f"width={WIDTH}",
            f"height={HEIGHT}",
            f"framerate={FPS}/1",
            "format=2",
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=I420",
            "!",
            "x264enc",
            "tune=zerolatency",
            "speed-preset=ultrafast",
            f"bitrate={BITRATE}",
            f"key-int-max={FPS}",
            "!",
            "h264parse",
            "config-interval=1",
            "!",
            "rtph264pay",
            "pt=97",
            "mtu=1400",
            "!",
            "udpsink",
            f"host={TARGET_HOST}",
            "port=5032",
            "sync=false",
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

except Exception as e:
    print(f"錯誤：無法啟動 GStreamer: {e}")
    sys.exit(1)

print("\n開始串流...")
print("按 Ctrl+C 停止\n")

frame_count = 0
error_count = 0

try:
    while True:
        ret, frame = cap.read()
        if not ret:
            error_count += 1
            if error_count > 10:
                print("\n錯誤：連續讀取失敗")
                break
            continue

        error_count = 0

        # 處理扁平化數組
        if frame.size == TOTAL_PIXELS:
            frame_2d = frame.reshape(HEIGHT, Y8I_WIDTH)
        elif frame.shape == (HEIGHT, Y8I_WIDTH):
            frame_2d = frame
        else:
            print(
                f"\n警告：尺寸不匹配 - 預期:{TOTAL_PIXELS}, 實際:{frame.size}, shape:{frame.shape}"
            )
            continue

        # Y8I 解交織
        left_ir = frame_2d[:, 0::2].copy()
        right_ir = frame_2d[:, 1::2].copy()

        # 驗證
        if left_ir.shape != (HEIGHT, WIDTH) or right_ir.shape != (HEIGHT, WIDTH):
            print(f"\n錯誤：解交織後尺寸錯誤 - 左:{left_ir.shape}, 右:{right_ir.shape}")
            continue

        # 發送
        try:
            gst_left.stdin.write(left_ir.tobytes())
            gst_right.stdin.write(right_ir.tobytes())
            gst_left.stdin.flush()
            gst_right.stdin.flush()
        except BrokenPipeError:
            print("\n錯誤：GStreamer pipeline 已斷開")
            break

        frame_count += 1
        if frame_count % FPS == 0:
            print(
                f"\r幀:{frame_count:5d} ({frame_count//FPS}秒) | "
                f"左:{left_ir.mean():5.1f} 右:{right_ir.mean():5.1f} | "
                f"Min/Max: {left_ir.min()}-{left_ir.max()}   ",
                end="",
                flush=True,
            )

except KeyboardInterrupt:
    print("\n\n收到停止信號")
except Exception as e:
    print(f"\n\n錯誤: {e}")
    import traceback

    traceback.print_exc()
finally:
    print("\n正在清理...")
    cap.release()

    try:
        gst_left.stdin.close()
        gst_right.stdin.close()
    except:
        pass

    gst_left.terminate()
    gst_right.terminate()

    try:
        gst_left.wait(timeout=2)
        gst_right.wait(timeout=2)
    except:
        gst_left.kill()
        gst_right.kill()

    print("完成！")
