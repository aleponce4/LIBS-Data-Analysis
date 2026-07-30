"""Platform and Tk runtime helpers."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tkinter as tk

from prolibspector.core.paths import resource_path


APP_DIR_NAME = "prolibspector"
WINDOWS_APP_USER_MODEL_ID = "Onteko.ProLIBSpector"
_WINDOWS_APP_USER_MODEL_ID_SET = False
_WINDOWS_HICON_CACHE: dict[tuple[str, int, int], int] = {}


def system_name() -> str:
    return platform.system()


def is_windows() -> bool:
    return system_name() == "Windows"


def is_linux() -> bool:
    return system_name() == "Linux"


def supports_thorlabs_ccs() -> bool:
    """Return whether the bundled Thorlabs CCS backend can run here."""
    return is_windows()


def configure_dpi_awareness() -> None:
    """Enable Windows process identity and DPI awareness when available."""
    configure_windows_app_user_model_id()
    from prolibspector.core.windowing import configure_process_dpi_awareness

    configure_process_dpi_awareness()


def configure_windows_app_user_model_id(app_id: str = WINDOWS_APP_USER_MODEL_ID) -> None:
    """Give script-launched Windows builds the app taskbar identity."""
    global _WINDOWS_APP_USER_MODEL_ID_SET
    if _WINDOWS_APP_USER_MODEL_ID_SET or not is_windows():
        return
    try:
        import ctypes

        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        setter(app_id)
        _WINDOWS_APP_USER_MODEL_ID_SET = True
    except Exception:
        pass


def maximize_window(window: "tk.Tk | tk.Toplevel") -> None:
    """Maximize a Tk window using the platform's safest available behavior."""
    try:
        if is_windows():
            window.state("zoomed")
            return
        window.attributes("-zoomed", True)
    except Exception:
        pass


def _load_windows_icon_handle(icon_path: str, width: int, height: int) -> int:
    """Load and cache a Windows HICON for taskbar/titlebar use."""
    key = (icon_path, width, height)
    cached = _WINDOWS_HICON_CACHE.get(key)
    if cached:
        return cached

    try:
        import ctypes

        image_icon = 1
        load_from_file = 0x00000010
        load_image = ctypes.windll.user32.LoadImageW
        load_image.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        load_image.restype = ctypes.c_void_p
        handle = load_image(None, icon_path, image_icon, width, height, load_from_file)
        if handle:
            _WINDOWS_HICON_CACHE[key] = int(handle)
            return int(handle)
    except Exception:
        pass
    return 0


def _set_windows_hwnd_icon(window: "tk.Tk | tk.Toplevel", icon_path: str) -> None:
    """Apply the app icon directly to the native Windows HWND.

    Tk's iconbitmap/iconphoto can set the titlebar icon but the Windows taskbar
    may still group a script-launched app under the Python executable icon. This
    native call updates the actual top-level HWND icon slots used by Explorer.
    """
    if not is_windows():
        return

    try:
        import ctypes

        hwnd = int(window.winfo_id())
        if hwnd <= 0:
            return
        user32 = ctypes.windll.user32
        get_system_metrics = user32.GetSystemMetrics
        get_system_metrics.argtypes = [ctypes.c_int]
        get_system_metrics.restype = ctypes.c_int
        send_message = user32.SendMessageW
        send_message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        send_message.restype = ctypes.c_void_p

        wm_seticon = 0x0080
        icon_small = 0
        icon_big = 1
        icon_small2 = 2
        sm_cxicon = 11
        sm_cyicon = 12
        sm_cxsmicon = 49
        sm_cysmicon = 50
        big_width = max(int(get_system_metrics(sm_cxicon)), 32)
        big_height = max(int(get_system_metrics(sm_cyicon)), 32)
        small_width = max(int(get_system_metrics(sm_cxsmicon)), 16)
        small_height = max(int(get_system_metrics(sm_cysmicon)), 16)
        big_icon = _load_windows_icon_handle(icon_path, big_width, big_height)
        small_icon = _load_windows_icon_handle(icon_path, small_width, small_height)

        for icon_slot, icon_handle in (
            (icon_big, big_icon),
            (icon_small, small_icon),
            (icon_small2, small_icon),
        ):
            if icon_handle:
                send_message(
                    ctypes.c_void_p(hwnd),
                    wm_seticon,
                    icon_slot,
                    ctypes.c_void_p(icon_handle),
                )
    except Exception:
        pass


def _set_window_icon_now(window: "tk.Tk | tk.Toplevel") -> None:
    """Apply the app icon once without scheduling a retry."""
    windows_icon_path = resource_path("Icons", "main_icon.ico")
    if is_windows():
        configure_windows_app_user_model_id()
        try:
            window.iconbitmap(default=windows_icon_path)
        except Exception:
            pass
        try:
            window.iconbitmap(windows_icon_path)
        except Exception:
            pass

    try:
        from PIL import Image, ImageTk

        image = Image.open(resource_path("Icons", "main_icon.png"))
        icon = ImageTk.PhotoImage(image)
        window.iconphoto(True, icon)
        window._prolibspector_icon = icon
    except Exception:
        pass

    if is_windows():
        _set_windows_hwnd_icon(window, windows_icon_path)


def _set_window_icon_if_alive(window: "tk.Tk | tk.Toplevel") -> None:
    try:
        if not window.winfo_exists():
            return
    except Exception:
        return
    _set_window_icon_now(window)


def _cancel_window_icon_afters(window: "tk.Tk | tk.Toplevel") -> None:
    after_ids = getattr(window, "_prolibspector_icon_after_ids", None)
    if not after_ids:
        return
    window._prolibspector_icon_after_ids = []
    for after_id in after_ids:
        try:
            window.after_cancel(after_id)
        except Exception:
            pass


def set_window_icon(window: "tk.Tk | tk.Toplevel") -> None:
    """Set the application icon and refresh it after Tk maps the window."""
    _set_window_icon_now(window)
    after_ids = []
    try:
        after_ids.append(window.after_idle(lambda win=window: _set_window_icon_if_alive(win)))
    except Exception:
        pass
    if is_windows():
        try:
            after_ids.append(window.after(250, lambda win=window: _set_window_icon_if_alive(win)))
        except Exception:
            pass
    window._prolibspector_icon_after_ids = after_ids
    if after_ids:
        try:
            window.bind(
                "<Destroy>",
                lambda event, win=window: event.widget is win and _cancel_window_icon_afters(win),
                add="+",
            )
        except Exception:
            pass


def user_data_dir() -> Path:
    """Return a per-user writable data directory."""
    if is_windows():
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "ProLIBSpector"
        return Path.home() / "AppData" / "Roaming" / "ProLIBSpector"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME
