# gst_utils.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeAlias

GST_AVAILABLE: bool = False
Gst: Any | None = None
GLib: Any | None = None


def load_gst() -> None:
    """Idempotent 初始化 GStreamer。"""
    global GST_AVAILABLE, Gst, GLib
    if GST_AVAILABLE:
        return
    try:
        import gi  # noqa: WPS433

        gi.require_version("Gst", "1.0")  # 必須先呼叫
        from gi.repository import GLib as _GLib
        from gi.repository import Gst as _Gst  # noqa: WPS433

        _Gst.init(None)
        Gst, GLib = _Gst, _GLib
        GST_AVAILABLE = True
    except Exception as e:
        GST_AVAILABLE = False
        raise RuntimeError(
            "GStreamer (PyGObject) not available. Install python3-gi and GI typelibs.\n"
            "Ubuntu: sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0"
        ) from e


def require_plugins(*names: str) -> None:
    """檢查必需的 GStreamer 外掛是否存在；缺少即丟錯。"""
    load_gst()
    assert Gst is not None
    missing = [n for n in names if Gst.ElementFactory.find(n) is None]
    if missing:
        raise RuntimeError(f"Missing GStreamer plugins: {', '.join(missing)}")


if TYPE_CHECKING:
    from gi.repository import Gst as GstType

    GstPipeline: TypeAlias = GstType.Pipeline
    GstElement: TypeAlias = GstType.Element
    GstFlowReturn: TypeAlias = GstType.FlowReturn
else:
    GstPipeline = Any  # 執行期/測試環境沒有 GI 時使用
    GstElement = Any
    GstFlowReturn = int
