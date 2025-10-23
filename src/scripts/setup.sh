#!/usr/bin/env bash
set -euo pipefail

# ===== paths =====
REPO_ROOT="$(pwd)"
echo "==> Changing to repo root: $REPO_ROOT"
cd "$REPO_ROOT"

echo "==> Repo: $REPO_ROOT"
[[ -f pyproject.toml ]] || { echo "ERROR: pyproject.toml not found in repo root"; exit 1; }

# ===== apt (GI/GStreamer) =====

if [[ -x "$REPO_ROOT/src/scripts/apt_install.sh" ]]; then
  echo "==> Running scripts/apt_install.sh ..."
  sudo bash "$REPO_ROOT/src/scripts/apt_install.sh"
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


# ===== pdm =====
if ! command -v pdm >/dev/null 2>&1; then
  echo "==> Installing PDM ..."
  python3 -m pip install --user -U pdm
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "==> PDM: $(pdm --version || true)"

# ===== helpers =====
venv_python() { echo "$REPO_ROOT/.venv/bin/python"; }
pdm_use_repo_venv() { pdm use -f "$(venv_python)"; }

gi_check() {
  pdm run python - <<'PY' || return 1
import sys
try:
    import gi
    gi.require_version("Gst","1.0")
    from gi.repository import Gst
    Gst.init(None)
    print("GI_OK in", sys.executable)
    raise SystemExit(0)
except Exception as e:
    print("GI_FAIL:", e)
    raise SystemExit(1)
PY
}

project_import_check() {
  pdm run python - <<'PY' || return 1
try:
    import utils, gst_realsense_launch  # editable install / PYTHONPATH
    print("IMPORT_OK:", utils.__file__, "|", gst_realsense_launch.__file__)
    raise SystemExit(0)
except Exception as e:
    print("IMPORT_FAIL:", e)
    raise SystemExit(1)
PY
}

# ===== venv (auto-detect) =====
VENV_DIR="$REPO_ROOT/.venv"
NEED_RECREATE=0

if [[ -d "$VENV_DIR" ]]; then
  echo "==> Found existing .venv"

  # try to use it
  pdm_use_repo_venv
  if gi_check; then
    echo "    GI/GStreamer OK in current .venv (will keep)"
  else
    echo "    GI check failed (will recreate .venv)"
    NEED_RECREATE=1
  fi

else
  echo "==> No .venv found (will create)"
fi

if [[ $NEED_RECREATE -eq 1 ]]; then
  echo "==> Recreating .venv (with system-site-packages)"
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR" --system-site-packages
  pdm_use_repo_venv
fi

# ===== deps sync =====
echo "==> pdm install (deps) ..."
pdm install

# ===== editable install (always ensure project is importable) =====
if ! project_import_check; then
  echo "==> pip install -e <repo> (editable) ..."
  pdm run pip install -e "$REPO_ROOT"
  project_import_check || { echo "ERROR: project import still failing"; exit 1; }
fi

# ===== final GI check (must pass) =====
echo "==> Verifying GI/GStreamer in final env ..."
gi_check

# ===== plugin check (best-effort) =====
echo "==> Checking common GStreamer plugins ..."
for p in udpsrc rtpjitterbuffer rtph264depay h264parse avdec_h264 videoconvert appsink; do
  if gst-inspect-1.0 "$p" >/dev/null 2>&1; then
    echo "  [+] $p"
  else
    echo "  [!] $p missing"
  fi
done

source "$REPO_ROOT/.venv/bin/activate"

echo "==> Done."
echo "Next:"
echo "  export PYTHONPATH=\"$REPO_ROOT/src\""
echo "  pdm run python -m gst_realsense_launch.startup.gst_sender --preset d435i --verbose"
echo "  pdm run python -m gst_realsense_launch.startup.gst_receiver --preset d435i"
