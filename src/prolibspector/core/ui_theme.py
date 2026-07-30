"""Shared UI color palettes for Tk canvases and Matplotlib surfaces."""

from __future__ import annotations

from prolibspector.core.settings import get_ui_settings
from prolibspector.core.ui_scale import apply_matplotlib_text_scale, current_ui_scale, scaled_int


LIGHT_PALETTE = {
    "canvas_bg": "#F7F9FC",
    "canvas_border": "#C8D0DA",
    "figure_bg": "#FFFFFF",
    "axes_bg": "#FFFFFF",
    "text": "#1E2B36",
    "muted_text": "#4D5A67",
    "grid": "#C8D0DA",
    "spine": "#6B7280",
    "spectrum_line": "#0078D4",
    "capture_line": "#FF4444",
    "plate_bg": "#F7F9FC",
    "plate_title": "#1E2B36",
    "plate_label": "#1E3140",
    "plate_arrow": "#1D6FB8",
    "well_empty": "#DDE5EE",
    "well_empty_outline": "#9BA8B4",
    "well_partial": "#F3C760",
    "well_partial_outline": "#A87A16",
    "well_done": "#55B97D",
    "well_done_outline": "#2F7E50",
    "well_repaired_outline": "#1F5E3C",
    "well_selected": "#DDEBFA",
    "well_selected_outline": "#1F5D96",
    "well_disabled": "#EEF2F5",
    "well_disabled_outline": "#C4CDD6",
    "well_text": "#14212B",
    "well_disabled_text": "#8A99A8",
    "current_well": "#D33232",
    "qc_pass": "#55B97D",
    "qc_pass_outline": "#2F7E50",
    "qc_warn": "#F3C760",
    "qc_warn_outline": "#A87A16",
    "qc_fail": "#E46A6A",
    "qc_fail_outline": "#9B1C1C",
    "error": "#A33",
}


DARK_PALETTE = {
    "canvas_bg": "#1C1C1C",
    "canvas_border": "#3A3A3A",
    "figure_bg": "#1C1C1C",
    "axes_bg": "#1C1C1C",
    "text": "#FAFAFA",
    "muted_text": "#B8B8B8",
    "grid": "#3A3A3A",
    "spine": "#6F6F6F",
    "spectrum_line": "#7DD3FC",
    "capture_line": "#F87171",
    "plate_bg": "#1C1C1C",
    "plate_title": "#FAFAFA",
    "plate_label": "#E6E6E6",
    "plate_arrow": "#60A5FA",
    "well_empty": "#2B2B2B",
    "well_empty_outline": "#6A6A6A",
    "well_partial": "#B88922",
    "well_partial_outline": "#FBBF24",
    "well_done": "#238655",
    "well_done_outline": "#5EE2A0",
    "well_repaired_outline": "#A7F3D0",
    "well_selected": "#1E3A5F",
    "well_selected_outline": "#57C8FF",
    "well_disabled": "#242424",
    "well_disabled_outline": "#464646",
    "well_text": "#F5F5F5",
    "well_disabled_text": "#777777",
    "current_well": "#F87171",
    "qc_pass": "#238655",
    "qc_pass_outline": "#5EE2A0",
    "qc_warn": "#B88922",
    "qc_warn_outline": "#FBBF24",
    "qc_fail": "#9F3434",
    "qc_fail_outline": "#FCA5A5",
    "error": "#F87171",
}


def current_theme_name() -> str:
    """Return the saved UI theme name normalized to Light or Dark."""
    return get_ui_settings()["theme"]


def get_palette(theme_name: str | None = None) -> dict[str, str]:
    """Return a color palette for the given or saved UI theme."""
    theme = (theme_name or current_theme_name()).title()
    return DARK_PALETTE if theme == "Dark" else LIGHT_PALETTE


def apply_canvas_theme(canvas, palette: dict[str, str] | None = None, *, border: bool = False) -> None:
    """Apply the current palette to a Tk canvas."""
    colors = palette or get_palette()
    canvas.configure(bg=colors["canvas_bg"])
    if border:
        canvas.configure(highlightbackground=colors["canvas_border"])


def apply_matplotlib_theme(fig, ax, line=None, palette: dict[str, str] | None = None) -> None:
    """Apply figure, axes, text, grid, and optional line colors."""
    colors = palette or get_palette()
    scale = apply_matplotlib_text_scale(current_ui_scale())

    def _scale_text(text, fallback: int) -> None:
        if text is None:
            return
        base = getattr(text, "_prolibspector_base_fontsize", None)
        if base is None:
            try:
                base = float(text.get_fontsize())
            except Exception:
                base = fallback
            setattr(text, "_prolibspector_base_fontsize", base)
        try:
            text.set_fontsize(scaled_int(base, scale))
        except Exception:
            pass

    fig.patch.set_facecolor(colors["figure_bg"])
    ax.set_facecolor(colors["axes_bg"])

    ax.xaxis.label.set_color(colors["text"])
    ax.yaxis.label.set_color(colors["text"])
    ax.title.set_color(colors["text"])
    _scale_text(ax.xaxis.label, 12)
    _scale_text(ax.yaxis.label, 12)
    _scale_text(ax.title, 12)
    ax.tick_params(axis="both", colors=colors["text"])
    for tick_label in [*ax.get_xticklabels(), *ax.get_yticklabels()]:
        _scale_text(tick_label, 10)
    legend = ax.get_legend()
    if legend is not None:
        for text in legend.get_texts():
            _scale_text(text, 10)

    for spine in ax.spines.values():
        spine.set_color(colors["spine"])

    ax.grid(which="both", linestyle="--", linewidth=0.5, color=colors["grid"], alpha=0.65)

    if line is not None:
        line.set_color(colors["spectrum_line"])
        line._normal_color = colors["spectrum_line"]
