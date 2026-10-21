# -*- coding: utf-8 -*-
"""Windows 高 DPI 感知（必须在 tk.Tk() 之前调用）。"""

from __future__ import annotations

import sys


def enable_windows_dpi_awareness() -> None:
    """开启 Per-Monitor DPI 感知，避免 100%-125% 缩放下字体模糊。"""
    if sys.platform != "win32":
        return

    import ctypes

    # Windows 10 1703+：Per-Monitor V2
    try:
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    except Exception:
        pass

    # Windows 8.1+：Per-Monitor
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    # Windows Vista+：System DPI aware
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
