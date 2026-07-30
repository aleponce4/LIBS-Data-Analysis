"""Shared text scaling helpers for Tk and Matplotlib UI surfaces."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


BASE_SCREEN_HEIGHT = 1080
MAX_UI_SCALE = 1.2
DEFAULT_FONT_FAMILY = "Segoe UI"

_CURRENT_UI_SCALE = 1.0


def ui_scale_for_screen_height(screen_height) -> float:
    """Return the app text scale for a screen height."""
    try:
        height = float(screen_height)
    except (TypeError, ValueError):
        return 1.0
    if height <= BASE_SCREEN_HEIGHT:
        return 1.0
    return min(MAX_UI_SCALE, max(1.0, height / BASE_SCREEN_HEIGHT))


def current_ui_scale() -> float:
    return _CURRENT_UI_SCALE


def ui_scale_for_widget(widget=None) -> float:
    """Return the scale associated with a widget's root, falling back to current scale."""
    if widget is None:
        return current_ui_scale()
    try:
        top = widget.winfo_toplevel()
    except (AttributeError, tk.TclError):
        return current_ui_scale()
    cached = getattr(top, "_prolibspector_ui_scale", None)
    if cached is not None:
        return float(cached)
    try:
        scale = ui_scale_for_screen_height(top.winfo_screenheight())
    except tk.TclError:
        scale = current_ui_scale()
    try:
        top._prolibspector_ui_scale = scale
    except Exception:
        pass
    return scale


def scaled_int(value: int | float, scale: float | None = None, *, minimum: int = 1) -> int:
    active_scale = current_ui_scale() if scale is None else float(scale)
    return max(minimum, int(round(float(value) * active_scale)))


def scaled_font(
    size: int | float,
    weight: str | None = None,
    *,
    family: str = DEFAULT_FONT_FAMILY,
    scale: float | None = None,
    widget=None,
) -> tuple:
    active_scale = ui_scale_for_widget(widget) if widget is not None else scale
    scaled_size = scaled_int(size, active_scale, minimum=1)
    if weight:
        return (family, scaled_size, weight)
    return (family, scaled_size)


def scaled_wrap(value: int | float, scale: float | None = None, *, minimum: int = 1, widget=None) -> int:
    active_scale = ui_scale_for_widget(widget) if widget is not None else scale
    return scaled_int(value, active_scale, minimum=minimum)


def scaled_padding(x: int | float, y: int | float, scale: float | None = None) -> tuple[int, int]:
    return (scaled_int(x, scale), scaled_int(y, scale))


def _scaled_font_size(size, scale: float) -> int:
    try:
        value = int(size)
    except (TypeError, ValueError):
        value = 9
    sign = -1 if value < 0 else 1
    return sign * scaled_int(abs(value), scale, minimum=1)


def _apply_named_fonts(root, scale: float) -> None:
    font_names = (
        "TkDefaultFont",
        "TkTextFont",
        "TkFixedFont",
        "TkMenuFont",
        "TkHeadingFont",
        "TkCaptionFont",
        "TkSmallCaptionFont",
        "TkIconFont",
    )
    base_specs = getattr(root, "_prolibspector_base_named_fonts", None)
    if base_specs is None:
        base_specs = {}
        root._prolibspector_base_named_fonts = base_specs

    for name in font_names:
        try:
            named_font = tkfont.nametofont(name, root=root)
        except tk.TclError:
            continue
        if name not in base_specs:
            base_specs[name] = named_font.actual()
        base = dict(base_specs[name])
        try:
            base["size"] = _scaled_font_size(base.get("size", 9), scale)
            named_font.configure(**base)
        except tk.TclError:
            continue


def _style_font(size: int, weight: str | None = None, *, family: str = DEFAULT_FONT_FAMILY, scale: float) -> tuple:
    return scaled_font(size, weight, family=family, scale=scale)


def _font_option(font_tuple: tuple) -> str:
    family = str(font_tuple[0])
    size = str(font_tuple[1])
    extras = " ".join(str(part) for part in font_tuple[2:])
    return f"{{{family}}} {size}{(' ' + extras) if extras else ''}"


def _configure_common_styles(root, scale: float) -> None:
    style = ttk.Style(root)
    default_font = _style_font(9, scale=scale)
    bold_font = _style_font(9, "bold", scale=scale)

    for style_name in (
        "TLabel",
        "TButton",
        "TCheckbutton",
        "TRadiobutton",
        "TEntry",
        "TCombobox",
        "TSpinbox",
        "TMenubutton",
        "TLabelframe.Label",
    ):
        style.configure(style_name, font=default_font)

    style.configure("Treeview", font=default_font, rowheight=scaled_int(24, scale))
    style.configure("Treeview.Heading", font=bold_font)

    style.configure("Title.TLabel", font=_style_font(20, "bold", scale=scale))
    style.configure("Subtitle.TLabel", font=_style_font(11, scale=scale))
    style.configure("ModeTitle.TLabel", font=_style_font(13, "bold", scale=scale))
    style.configure("ModeDesc.TLabel", font=default_font, wraplength=scaled_wrap(240, scale))

    style.configure("Sidebar.TLabelframe.Label", font=_style_font(10, "bold", scale=scale))
    style.configure("LeftAligned.TButton", anchor="w", font=default_font, padding=scaled_padding(8, 5, scale))
    style.configure("Sidebar.TButton", font=default_font)
    style.configure("Sidebar.TCheckbutton", font=default_font)
    style.configure("Sidebar.TEntry", font=default_font)
    style.configure("Sidebar.TSpinbox", font=default_font)
    style.configure("Status.TLabel", font=default_font)
    style.configure("StatusValue.TLabel", font=bold_font)
    style.configure("AnalysisStatus.TLabel", font=default_font)
    style.configure("Emphasized.TLabel", font=_style_font(16, "bold", scale=scale))

    style.configure("AutomationSummary.TLabel", font=default_font)
    style.configure("AutomationStep.TLabel", font=default_font)
    style.configure("AutomationPanelTitle.TLabel", font=bold_font)
    style.configure("AutomationPanel.TButton", font=default_font, padding=scaled_padding(8, 3, scale))
    style.configure("AutomationQcMetricTitle.TLabel", font=_style_font(9, "bold", scale=scale))
    style.configure("AutomationQcMetricValue.TLabel", font=_style_font(11, "bold", scale=scale))
    style.configure("RunConsole.TLabelframe.Label", font=_style_font(11, "bold", scale=scale))
    style.configure("RunPrimary.TButton", font=_style_font(11, "bold", scale=scale), padding=scaled_padding(12, 10, scale))


def apply_text_scale(root, *, screen_height=None) -> float:
    """Apply app-wide text scaling for a Tk interpreter without changing Tk pixel scaling."""
    global _CURRENT_UI_SCALE
    if screen_height is None:
        try:
            screen_height = root.winfo_screenheight()
        except (AttributeError, tk.TclError):
            screen_height = None
    scale = ui_scale_for_screen_height(screen_height)
    _CURRENT_UI_SCALE = scale
    try:
        root._prolibspector_ui_scale = scale
    except Exception:
        pass

    _apply_named_fonts(root, scale)
    _configure_common_styles(root, scale)
    try:
        root.option_add("*Font", _font_option(scaled_font(9, scale=scale)))
        root.option_add("*Menu.font", _font_option(scaled_font(9, scale=scale)))
        root.option_add("*Text.font", _font_option(scaled_font(9, scale=scale)))
    except tk.TclError:
        pass
    return scale


def apply_matplotlib_text_scale(scale: float | None = None) -> float:
    """Apply current text scale to Matplotlib defaults used by embedded plots."""
    active_scale = current_ui_scale() if scale is None else float(scale)
    try:
        import matplotlib

        matplotlib.rcParams["font.family"] = "DejaVu Sans"
        matplotlib.rcParams["font.size"] = 12
        matplotlib.rcParams["axes.titlesize"] = 12
        matplotlib.rcParams["axes.labelsize"] = 12
        matplotlib.rcParams["xtick.labelsize"] = 10
        matplotlib.rcParams["ytick.labelsize"] = 10
        matplotlib.rcParams["legend.fontsize"] = 10
    except Exception:
        pass
    return active_scale
