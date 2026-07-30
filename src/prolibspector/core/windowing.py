"""Window sizing, placement, and DPI helpers for Tk surfaces."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import platform
import re
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from prolibspector.core.ui_scale import scaled_int, ui_scale_for_widget
from prolibspector.core.ui_theme import apply_canvas_theme

if TYPE_CHECKING:
    from collections.abc import Callable


DEFAULT_MARGIN = 20


@dataclass(frozen=True)
class ScreenBounds:
    """Visible monitor bounds in Tk screen coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def configure_process_dpi_awareness() -> None:
    """Prefer per-monitor DPI awareness on Windows before creating Tk roots."""
    if platform.system() != "Windows":
        return

    try:
        # Windows 10 Anniversary Update and newer.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass

    try:
        # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _fallback_screen_bounds(widget: tk.Misc) -> ScreenBounds:
    return ScreenBounds(
        0,
        0,
        max(1, int(widget.winfo_screenwidth())),
        max(1, int(widget.winfo_screenheight())),
    )


def _is_mapped(widget: tk.Misc | None) -> bool:
    if widget is None:
        return False
    try:
        return bool(widget.winfo_ismapped())
    except tk.TclError:
        return False


def _widget_center(widget: tk.Misc) -> tuple[int, int]:
    try:
        widget.update_idletasks()
        x = int(widget.winfo_rootx())
        y = int(widget.winfo_rooty())
        width = max(1, int(widget.winfo_width()))
        height = max(1, int(widget.winfo_height()))
        return x + width // 2, y + height // 2
    except Exception:
        return int(widget.winfo_pointerx()), int(widget.winfo_pointery())


def _windows_monitor_bounds_for_point(x: int, y: int) -> ScreenBounds | None:
    if platform.system() != "Windows":
        return None

    try:
        user32 = ctypes.windll.user32

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        monitor = user32.MonitorFromPoint(POINT(x, y), 2)
        if not monitor:
            return None

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None

        rect = info.rcWork
        return ScreenBounds(
            int(rect.left),
            int(rect.top),
            max(1, int(rect.right - rect.left)),
            max(1, int(rect.bottom - rect.top)),
        )
    except Exception:
        return None


def screen_bounds_for(widget: tk.Misc | None) -> ScreenBounds:
    """Return visible bounds for the monitor containing ``widget``."""
    if widget is None:
        root = tk._default_root
        if root is None:
            return ScreenBounds(0, 0, 1, 1)
        widget = root

    x, y = _widget_center(widget)
    return _windows_monitor_bounds_for_point(x, y) or _fallback_screen_bounds(widget)


def clamp_window_size(
    preferred: tuple[int, int] | None,
    min_size: tuple[int, int] | None,
    bounds: ScreenBounds,
    max_fraction: tuple[float, float] = (0.92, 0.88),
) -> tuple[int, int]:
    """Clamp a requested window size to usable monitor bounds."""
    pref_w, pref_h = preferred or (bounds.width, bounds.height)
    min_w, min_h = min_size or (1, 1)
    max_w = max(1, int(bounds.width * max_fraction[0]))
    max_h = max(1, int(bounds.height * max_fraction[1]))

    eff_min_w = min(max(1, int(min_w)), max_w)
    eff_min_h = min(max(1, int(min_h)), max_h)
    width = max(eff_min_w, min(int(pref_w), max_w))
    height = max(eff_min_h, min(int(pref_h), max_h))
    return width, height


def clamp_position(
    x: int,
    y: int,
    width: int,
    height: int,
    bounds: ScreenBounds,
    margin: int = DEFAULT_MARGIN,
) -> tuple[int, int]:
    """Clamp a top-left coordinate so the window remains visible."""
    if width + margin * 2 >= bounds.width:
        clamped_x = bounds.x
    else:
        clamped_x = max(bounds.x + margin, min(int(x), bounds.right - width - margin))

    if height + margin * 2 >= bounds.height:
        clamped_y = bounds.y
    else:
        clamped_y = max(bounds.y + margin, min(int(y), bounds.bottom - height - margin))

    return clamped_x, clamped_y


def centered_position(
    width: int,
    height: int,
    bounds: ScreenBounds,
    parent: tk.Misc | None = None,
) -> tuple[int, int]:
    """Return a centered, clamped position over parent or screen bounds."""
    if parent is not None:
        try:
            parent.update_idletasks()
            if not parent.winfo_ismapped():
                raise tk.TclError("parent is not mapped")
            px = int(parent.winfo_rootx())
            py = int(parent.winfo_rooty())
            pw = max(1, int(parent.winfo_width()))
            ph = max(1, int(parent.winfo_height()))
            return clamp_position(px + (pw - width) // 2, py + (ph - height) // 2, width, height, bounds)
        except Exception:
            pass

    return clamp_position(bounds.x + (bounds.width - width) // 2, bounds.y + (bounds.height - height) // 2, width, height, bounds)


def safe_set_geometry(
    window: tk.Tk | tk.Toplevel,
    preferred: tuple[int, int] | None = None,
    parent: tk.Misc | None = None,
    min_size: tuple[int, int] | None = None,
    max_fraction: tuple[float, float] = (0.92, 0.88),
    center: bool = True,
) -> tuple[int, int]:
    """Apply a clamped geometry to a Tk window and return the final size."""
    anchor = parent or window
    bounds = screen_bounds_for(anchor)
    width, height = clamp_window_size(preferred, min_size, bounds, max_fraction)
    if min_size:
        min_w, min_h = clamp_window_size(min_size, min_size, bounds, max_fraction)
        window.minsize(min_w, min_h)

    if center:
        x, y = centered_position(width, height, bounds, parent)
        window.geometry(f"{width}x{height}+{x}+{y}")
    else:
        window.geometry(f"{width}x{height}")
    return width, height


def center_on_parent_or_screen(window: tk.Tk | tk.Toplevel, parent: tk.Misc | None = None) -> None:
    """Center an already-sized window and clamp it to visible monitor bounds."""
    window.update_idletasks()
    width = max(1, int(window.winfo_width()))
    height = max(1, int(window.winfo_height()))
    bounds = screen_bounds_for(parent or window)
    x, y = centered_position(width, height, bounds, parent)
    window.geometry(f"+{x}+{y}")


def _remembered_window_size(remember_key: str) -> tuple[int, int] | None:
    from prolibspector.core.settings import get_ui_pref

    raw = get_ui_pref(f"geom_{remember_key}", "")
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _persist_window_size_on_close(window: tk.Tk | tk.Toplevel, remember_key: str) -> None:
    def _save(event):
        if event.widget is not window:
            return
        try:
            width, height = window.winfo_width(), window.winfo_height()
        except tk.TclError:
            return
        if width > 50 and height > 50:
            from prolibspector.core.settings import save_ui_prefs

            save_ui_prefs({f"geom_{remember_key}": f"{width}x{height}"})

    try:
        window.bind("<Destroy>", _save, add="+")
    except (AttributeError, tk.TclError):
        pass


def apply_window_policy(
    window: tk.Tk | tk.Toplevel,
    parent: tk.Misc | None = None,
    preferred: tuple[int, int] | None = None,
    min_size: tuple[int, int] | None = None,
    max_fraction: tuple[float, float] = (0.92, 0.88),
    modal: bool = False,
    resizable: bool = True,
    remember_key: str | None = None,
    center: bool = True,
) -> tuple[int, int]:
    """Finalize sizing, placement, and common behavior for a Tk window.

    When *remember_key* is given, the window reopens at its last-closed size
    (still clamped to the screen); the size is stored with the UI prefs and
    honours the same persistence kill switch."""
    window.resizable(resizable, resizable)
    if parent is not None and isinstance(window, tk.Toplevel) and _is_mapped(parent):
        try:
            window.transient(parent)
        except tk.TclError:
            pass

    window.update_idletasks()
    requested = preferred or (window.winfo_reqwidth(), window.winfo_reqheight())
    if remember_key:
        remembered = _remembered_window_size(remember_key)
        if remembered is not None and resizable:
            requested = remembered
        _persist_window_size_on_close(window, remember_key)
    final_size = safe_set_geometry(window, requested, parent=parent, min_size=min_size, max_fraction=max_fraction, center=center)

    if modal and isinstance(window, tk.Toplevel):
        try:
            window.grab_set()
        except tk.TclError:
            pass

    if isinstance(window, tk.Toplevel):
        try:
            window.deiconify()
            window.lift()
            window.focus_force()
        except tk.TclError:
            pass

    return final_size


def create_dialog(
    parent: tk.Misc | None,
    title: str,
    modal: bool = False,
    resizable: bool = True,
) -> tk.Toplevel:
    """Create a Toplevel with common parent/modal metadata."""
    window = tk.Toplevel(parent) if parent is not None else tk.Toplevel()
    window.title(title)
    window.resizable(resizable, resizable)
    try:
        from prolibspector.core.runtime import set_window_icon

        set_window_icon(window)
    except Exception:
        pass
    try:
        window._prolibspector_ui_scale = ui_scale_for_widget(parent or window)
    except Exception:
        pass
    if parent is not None and _is_mapped(parent):
        try:
            window.transient(parent)
        except tk.TclError:
            pass
    if modal:
        window._prolibspector_modal = True

    def _close_on_escape(_event=None):
        # Defer to the dialog's own close handler when one is installed so
        # Escape behaves exactly like the window's X button.
        try:
            command = window.protocol("WM_DELETE_WINDOW")
            if command:
                window.tk.eval(command)
            else:
                window.destroy()
        except tk.TclError:
            pass

    try:
        window.bind("<Escape>", _close_on_escape)
    except (AttributeError, tk.TclError):
        pass
    return window


class ScrollableFrame(ttk.Frame):
    """A scrollable frame with a stable ``content`` child for dialog bodies."""

    def __init__(self, master, *, orient: str = "vertical", **kwargs):
        super().__init__(master, **kwargs)
        self._orient = orient
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        apply_canvas_theme(self.canvas)
        self.content = ttk.Frame(self.canvas)
        self._window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.vscrollbar = None
        self.hscrollbar = None
        if orient in ("vertical", "both"):
            self.vscrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.vscrollbar.set)
            self.vscrollbar.grid(row=0, column=1, sticky="ns")
        if orient in ("horizontal", "both"):
            self.hscrollbar = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=self.hscrollbar.set)
            self.hscrollbar.grid(row=1, column=0, sticky="ew")

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_content_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        if self._orient == "vertical":
            self.canvas.itemconfigure(self._window_id, width=event.width)


def make_scrollable_frame(parent, *, orient: str = "vertical") -> tuple[ScrollableFrame, ttk.Frame]:
    """Return ``(outer, content)`` for scrollable dialog content."""
    outer = ScrollableFrame(parent, orient=orient)
    return outer, outer.content


def bind_dynamic_wrap(label: tk.Misc, *, padding: int = 40, minimum: int = 180) -> None:
    """Keep a label wraplength aligned with its available width."""

    def _refresh(event=None) -> None:
        scale = ui_scale_for_widget(label)
        width = getattr(event, "width", None) or label.winfo_width()
        label.configure(wraplength=max(scaled_int(minimum, scale), int(width) - scaled_int(padding, scale)))

    label.bind("<Configure>", _refresh, add="+")
    label.after_idle(_refresh)


def make_markdown_help_window(
    parent: tk.Misc | None,
    title: str,
    html_factory: "Callable[[], str]",
    html_label_factory: "Callable[[tk.Misc, str], tk.Misc]",
) -> tk.Toplevel:
    """Create a large, resizable, scrollable help window."""
    window = create_dialog(parent, title, modal=False, resizable=True)
    outer, content = make_scrollable_frame(window)
    outer.pack(fill=tk.BOTH, expand=True)
    html_label = html_label_factory(content, html_factory())
    html_label.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
    window.bind("<Escape>", lambda _event: window.destroy())
    apply_window_policy(window, parent=parent, preferred=(980, 760), min_size=(640, 420), modal=False, resizable=True)
    return window
