#!/usr/bin/env bash
set -euo pipefail

# --- paths ---
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Repo: $REPO_ROOT"

# --- APT: GI / GStreamer / 基本工具 ---
if [[ -x "scripts/apt_install.sh" ]]; then
  echo "==> Running scripts/apt_install.sh ..."
  sudo bash scripts/apt_install.sh
else
  echo "==> Installing system packages (GI + GStreamer) ..."
  sudo apt update
  sudo apt install -y \
    python3-gi \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
    gstreamer1.0-tools \
    build-essential python3-venv
fi

# --- PDM 檢查 ---
if ! command -v pdm >/dev/null 2>&1; then
  echo "==> Installing PDM for current user ..."
  python3 -m pip install --user -U pdm
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> PDM version: $(pdm --version || true)"

# --- 建立/使用 .venv（含 system-site-packages，才能看到 python3-gi）---
VENV_DIR="$REPO_ROOT/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating venv at .venv (with system site-packages) ..."
  python3 -m venv "$VENV_DIR" --system-site-packages
fi
VENV_PY="$VENV_DIR/bin/python"

echo "==> Pointing PDM to $VENV_PY"
pdm use -f "$VENV_PY"

# --- 以 PDM 同步專案依賴 ---
echo "==> pdm install (project deps) ..."
pdm install

# --- 安裝專案本身（editable），把 gst_realsense_launch 與 utils 裝進 venv ---
echo "==> pip install -e . (editable) ..."
pdm run pip install -e .

# --- 驗證 GI / GStreamer 可用 ---
echo "==> Verifying GI / GStreamer ..."
pdm run python - <<'PY'
import sys
try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    print("OK: GI/GStreamer usable in", sys.executable)
except Exception as e:
    print("ERROR: GI/GStreamer not usable in", sys.executable)
    print(e)
    raise SystemExit(1)
PY

echo "==> Checking common GStreamer plugins (best effort) ..."
for p in udpsrc rtpjitterbuffer rtph264depay h264parse avdec_h264 videoconvert appsink; do
  if gst-inspect-1.0 "$p" >/dev/null 2>&1; then
    echo "  [+] $p"
  else
    echo "  [!] $p missing (check your GStreamer installation)"
  fi
done

echo "==> Done."
echo "Next:"
echo "  1) Export PYTHONPATH (若未設置 __init__ 自動注入)："
echo "     export PYTHONPATH=\"$REPO_ROOT/src\""
echo "  2) Run:"
echo "     pdm run python -m gst_realsense_launch.startup.gst_sender --preset d435i --verbose"
echo "     pdm run python -m gst_realsense_launch.startup.nvblox_receiver --preset d435i"
