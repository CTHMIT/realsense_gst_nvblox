#!/usr/bin/env python3
"""
RealSense 雙路紅外線接收端
自動適應任何解析度
"""
import signal
import subprocess
import sys
import time

# ==================== 配置區 ====================
LEFT_PORT = 5031
RIGHT_PORT = 5032
DISPLAY_MODE = "window"  # 'window', 'side-by-side', 'none'

# 顯示選項
# DISPLAY_MODE = 'window'        # 兩個獨立視窗
# DISPLAY_MODE = 'side-by-side'  # 左右並排顯示
# DISPLAY_MODE = 'none'          # 不顯示（純解碼測試）
# ================================================

processes: list = []


def signal_handler(sig, frame):
    print("\n\n正在停止接收...")
    for p in processes:
        try:
            p.terminate()
        except:
            pass
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)

print("=" * 60)
print("RealSense D435i 雙路紅外線接收端")
print("=" * 60)
print(f"左紅外線: UDP port {LEFT_PORT}")
print(f"右紅外線: UDP port {RIGHT_PORT}")
print(f"顯示模式: {DISPLAY_MODE}")
print("=" * 60)
print("按 Ctrl+C 停止")
print()


def create_receiver(port, name, window_x=0, window_y=0):
    """創建接收 pipeline"""

    if DISPLAY_MODE == "window":
        # 獨立視窗顯示
        pipeline = [
            "gst-launch-1.0",
            "-v",
            "udpsrc",
            f"port={port}",
            "caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96",
            "!",
            "rtph264depay",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "videoconvert",
            "!",
            "videoscale",
            "!",
            "video/x-raw",
            "!",
            "autovideosink",
            f"sync=false",
        ]

    elif DISPLAY_MODE == "side-by-side":
        # 使用 ximagesink 可以設置位置
        pipeline = [
            "gst-launch-1.0",
            "-v",
            "udpsrc",
            f"port={port}",
            "caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96",
            "!",
            "rtph264depay",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "videoconvert",
            "!",
            "ximagesink",
            f"sync=false",
        ]

    elif DISPLAY_MODE == "none":
        # 不顯示，只解碼（測試用）
        pipeline = [
            "gst-launch-1.0",
            "-v",
            "udpsrc",
            f"port={port}",
            "caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96",
            "!",
            "rtph264depay",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            "fakesink",
            "sync=false",
        ]

    return subprocess.Popen(pipeline, stderr=subprocess.PIPE)


try:
    # 啟動左紅外線接收
    print("啟動左紅外線接收...")
    left_proc = create_receiver(LEFT_PORT, "left_ir", window_x=0)
    processes.append(left_proc)
    time.sleep(0.5)

    # 啟動右紅外線接收
    print("啟動右紅外線接收...")
    right_proc = create_receiver(RIGHT_PORT, "right_ir", window_x=640)
    processes.append(right_proc)

    print("\n接收中...\n")

    # 保持運行
    while True:
        time.sleep(1)

        # 檢查 process 是否還活著
        if left_proc.poll() is not None:
            print("警告：左紅外線 pipeline 已停止")
        if right_proc.poll() is not None:
            print("警告：右紅外線 pipeline 已停止")

except KeyboardInterrupt:
    print("\n\n收到停止信號")
except Exception as e:
    print(f"錯誤: {e}")
finally:
    print("正在清理...")
    for p in processes:
        try:
            p.terminate()
            p.wait(timeout=2)
        except:
            try:
                p.kill()
            except:
                pass
    print("完成！")
