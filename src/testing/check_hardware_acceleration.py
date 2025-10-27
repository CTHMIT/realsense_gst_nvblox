#!/usr/bin/env python3
"""Check available hardware acceleration for GStreamer H.264 decoding"""

import subprocess
import sys


def check_gstreamer_element(element_name):
    """Check if a GStreamer element is available"""
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", element_name], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except:
        return False


def check_vaapi():
    """Check for VAAPI (Intel/AMD) support"""
    print("\n🔍 Checking VAAPI (Intel/AMD GPU acceleration)...")

    # Check for VAAPI device
    vaapi_device = subprocess.run(["ls", "/dev/dri/"], capture_output=True, text=True)

    has_device = "renderD" in vaapi_device.stdout
    has_decoder = check_gstreamer_element("vaapih264dec")

    if has_device and has_decoder:
        print("   ✅ VAAPI available - Hardware acceleration ready!")
        print("   📦 GStreamer plugin: gstreamer1.0-vaapi")
        return True
    elif has_device and not has_decoder:
        print("   ⚠️  VAAPI device found but decoder missing")
        print("   📦 Install: sudo apt install gstreamer1.0-vaapi")
        return False
    else:
        print("   ❌ VAAPI not available (no compatible GPU)")
        return False


def check_nvdec():
    """Check for NVDEC (NVIDIA) support"""
    print("\n🔍 Checking NVDEC (NVIDIA GPU acceleration)...")

    # Check for NVIDIA GPU
    nvidia_check = subprocess.run(["nvidia-smi"], capture_output=True, text=True)

    has_gpu = nvidia_check.returncode == 0
    has_decoder = check_gstreamer_element("nvh264dec")

    if has_gpu and has_decoder:
        print("   ✅ NVDEC available - Hardware acceleration ready!")
        print("   📦 GStreamer plugin: gstreamer1.0-plugins-bad")
        return True
    elif has_gpu and not has_decoder:
        print("   ⚠️  NVIDIA GPU found but decoder missing")
        print("   📦 Install: sudo apt install gstreamer1.0-plugins-bad")
        return False
    else:
        print("   ❌ NVDEC not available (no NVIDIA GPU)")
        return False


def check_software_decoder():
    """Check software decoder"""
    print("\n🔍 Checking software decoder (fallback)...")

    has_decoder = check_gstreamer_element("avdec_h264")

    if has_decoder:
        print("   ✅ Software decoder available (avdec_h264)")
        print("   ⚠️  Note: Software decoding is slower than hardware")
        return True
    else:
        print("   ❌ Software decoder missing!")
        print("   📦 Install: sudo apt install gstreamer1.0-libav")
        return False


def check_system_optimization():
    """Check system-level optimizations"""
    print("\n🔍 Checking system optimizations...")

    # Check CPU governor
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
            governor = f.read().strip()

        if governor == "performance":
            print("   ✅ CPU governor: performance")
        else:
            print(f"   ⚠️  CPU governor: {governor} (consider 'performance')")
            print("   💡 Set: sudo cpupower frequency-set -g performance")
    except:
        print("   ℹ️  Cannot check CPU governor")

    # Check UDP buffer sizes
    try:
        with open("/proc/sys/net/core/rmem_max") as f:
            rmem_max = int(f.read().strip())

        if rmem_max >= 134217728:  # 128MB
            print(f"   ✅ UDP receive buffer: {rmem_max // 1024 // 1024}MB")
        else:
            print(f"   ⚠️  UDP receive buffer: {rmem_max // 1024 // 1024}MB (small)")
            print("   💡 Increase: sudo sysctl -w net.core.rmem_max=134217728")
    except:
        print("   ℹ️  Cannot check UDP buffer size")


def get_recommendation():
    """Provide optimization recommendation"""
    print("\n" + "=" * 60)
    print("📊 PERFORMANCE OPTIMIZATION SUMMARY")
    print("=" * 60)

    vaapi = check_vaapi()
    nvdec = check_nvdec()
    software = check_software_decoder()

    print()

    if vaapi or nvdec:
        print("✅ Hardware acceleration available!")
        print("   Expected FPS improvement: 2-3x faster decoding")
        print("   Your optimized receiver will automatically use hardware decoding")
    elif software:
        print("⚠️  Only software decoding available")
        print("   Consider installing VAAPI or using NVIDIA GPU for better performance")
        print("   Expected baseline: ~30-40 FPS (640x480)")
    else:
        print("❌ No H.264 decoder available!")
        print("   Install required GStreamer plugins")

    check_system_optimization()

    print("\n💡 QUICK WINS:")
    print("   1. Use hardware decoder (if available)")
    print("   2. Reduce latency: jitter_buffer.depth.latency: 50")
    print("   3. Increase UDP buffer: streaming.udp.buffer_size: 30000000")
    print("   4. Optimize threads: streaming.processing.max_threads: 8")
    print("   5. Lower resolution if needed: 320x240 = 4x faster")

    print("\n📦 Installation commands:")
    if not vaapi and not nvdec:
        print("   # For Intel/AMD GPU:")
        print("   sudo apt install gstreamer1.0-vaapi")
        print()
        print("   # For NVIDIA GPU:")
        print("   sudo apt install gstreamer1.0-plugins-bad")

    print("=" * 60)


def main():
    print("=" * 60)
    print("🎯 GStreamer Hardware Acceleration Check")
    print("=" * 60)

    # Check GStreamer installation
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print("✅ GStreamer is installed")
        else:
            print("❌ GStreamer not found")
            sys.exit(1)
    except:
        print("❌ GStreamer not found")
        print("   Install: sudo apt install gstreamer1.0-tools")
        sys.exit(1)

    get_recommendation()


if __name__ == "__main__":
    main()
