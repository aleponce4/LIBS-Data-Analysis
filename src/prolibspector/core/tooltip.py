"""Minimal hover tooltips for Tk/ttk widgets.

Usage: ``attach_tooltip(widget, "text")`` or pass a zero-arg callable for
dynamic text (e.g. the current reason a button is disabled). The tooltip
reads the palette at show time, so it follows light/dark theme switches.
"""

from __future__ import annotations

import tkinter as tk

from prolibspector.core.ui_theme import get_palette

_SHOW_DELAY_MS = 600
_WRAP_PX = 320


def attach_tooltip(widget, text, *, delay_ms: int = _SHOW_DELAY_MS) -> None:
    """Show *text* in a small hover window near *widget*.

    *text* may be a string or a zero-arg callable returning one; an empty
    result suppresses the tooltip (useful for conditional hints)."""
    state = {"after": None, "window": None}

    def _resolve_text() -> str:
        try:
            value = text() if callable(text) else text
        except Exception:
            return ""
        return str(value or "").strip()

    def _show():
        state["after"] = None
        message = _resolve_text()
        if not message or state["window"] is not None:
            return
        try:
            palette = get_palette()
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            label = tk.Label(
                tip,
                text=message,
                bg=palette["canvas_bg"],
                fg=palette["text"],
                relief="solid",
                borderwidth=1,
                justify="left",
                wraplength=_WRAP_PX,
                padx=8,
                pady=4,
            )
            label.pack()
            x = widget.winfo_rootx() + 12
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            tip.wm_geometry(f"+{x}+{y}")
            state["window"] = tip
        except tk.TclError:
            state["window"] = None

    def _hide(_event=None):
        after_id, state["after"] = state["after"], None
        if after_id is not None:
            try:
                widget.after_cancel(after_id)
            except tk.TclError:
                pass
        window, state["window"] = state["window"], None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def _schedule(_event=None):
        _hide()
        try:
            state["after"] = widget.after(delay_ms, _show)
        except tk.TclError:
            state["after"] = None

    widget.bind("<Enter>", _schedule, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<ButtonPress>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")
