# src/utils/gst_utils.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Tuple, TypeAlias

from utils.logger import LOGGER

GST_AVAILABLE: bool = False
Gst: Any | None = None
GLib: Any | None = None


def load_gst() -> tuple[Any, Any, bool]:
    """Idempotent init GStreamer"""
    global GST_AVAILABLE, Gst, GLib
    if GST_AVAILABLE:
        return Gst, GLib, GST_AVAILABLE
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib as _GLib
        from gi.repository import Gst as _Gst

        _Gst.init(None)
        Gst, GLib = _Gst, _GLib
        GST_AVAILABLE = True
        return Gst, GLib, GST_AVAILABLE

    except Exception as e:
        GST_AVAILABLE = False
        LOGGER.error("GStreamer (PyGObject) not available. Install python3-gi and GI typelibs.")
        LOGGER.error(f"Error details: {e}")
        return None, None, GST_AVAILABLE


def require_plugins(*names: str) -> None:
    """check that the given GStreamer plugins are available"""
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
    GstPipeline = Any
    GstElement = Any
    GstFlowReturn = int


if __name__ == "__main__":

    Gst, GLib, GST_AVAILABLE = load_gst()
    if GST_AVAILABLE:
        LOGGER.info("GStreamer successfully loaded.")
    else:
        LOGGER.error("GStreamer failed to load.")
