"""Live-updating Matplotlib canvas helpers for Acquisition Mode."""

# Optimized for real-time display using set_ydata() + draw_idle() instead of full redraws.

import tkinter as tk
from tkinter import ttk
import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends._backend_tk import NavigationToolbar2Tk
from matplotlib.figure import Figure
import numpy as np

from prolibspector.core.spectrum_display import spectrum_autoscale_peak
from prolibspector.core.ui_scale import apply_matplotlib_text_scale
from prolibspector.core.ui_theme import apply_matplotlib_theme, get_palette

# Consistent font with the analysis mode
apply_matplotlib_text_scale()

LIVE_SPECTRUM_MIN_Y_TOP = 2000.0


def _apply_live_spectrum_data(ax, line, wavelengths, intensities):
    line.set_xdata(wavelengths)
    line.set_ydata(intensities)

    # Update X limits to match actual data range
    if len(wavelengths) > 0:
        ax.set_xlim([wavelengths[0], wavelengths[-1]])

    # --- Hysteresis Y-axis auto-scaling (skipped while the user holds Y) ---
    if len(intensities) > 0 and not getattr(ax, "_hold_y_autoscale", False):
        peak = spectrum_autoscale_peak(wavelengths, intensities)
        current_top = ax.get_ylim()[1]

        # Minimum floor keeps dark noise visible without letting it fill the view.
        min_ylim = LIVE_SPECTRUM_MIN_Y_TOP

        target_top = min_ylim if peak <= min_ylim else peak * 1.3
        needs_rescale = False
        if peak > current_top * 0.80:
            # Signal approaching the top - zoom out
            needs_rescale = True
        elif current_top > target_top * 1.50:
            # View is much larger than the useful signal range - zoom in.
            needs_rescale = True

        if needs_rescale:
            new_top = target_top
            ax.set_ylim([0, new_top])


class CustomToolbar(NavigationToolbar2Tk):
    """Toolbar without the Save button (export is handled by the sidebar)."""
    toolitems = [t for t in NavigationToolbar2Tk.toolitems if t[0] != 'Save']


def on_resize(event, canvas):
    canvas.draw()


def create_acquisition_graph(parent_frame):
    """
    Create the Matplotlib figure and canvas for live spectrum display.
    
    Axis limits use sensible defaults (200–1000 nm, 0–65535 counts) that are
    updated dynamically when a spectrometer connects — see
    ``configure_graph_for_device()``.
    
    Args:
        parent_frame: The Tkinter frame to embed the graph into.
        
    Returns:
        tuple: (graph_frame, fig, ax, canvas, line)
            - graph_frame: The containing frame
            - fig: Matplotlib Figure
            - ax: Matplotlib Axes
            - canvas: FigureCanvasTkAgg instance
            - line: The Line2D object for fast updates via set_ydata()
    """
    apply_matplotlib_text_scale()
    palette = get_palette()
    graph_frame = tk.Frame(parent_frame, bg=palette["canvas_bg"])
    graph_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # Create an unregistered Figure for Tk embedding. Using pyplot here would
    # retain every GUI-test graph in pyplot's global figure manager.
    fig = Figure(figsize=(14, 8))
    ax = fig.add_subplot(111)
    fig.subplots_adjust(left=0.1)

    # Default axis setup — will be reconfigured by configure_graph_for_device()
    ax.set_xlim([200, 1000])
    ax.set_ylim([0, 65535])
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Intensity (counts)")
    ax.set_title("Live Spectrum")
    apply_matplotlib_theme(fig, ax, palette=palette)

    # Pre-create a line with placeholder data for fast updates
    x_placeholder = np.linspace(200, 1000, 2048)
    y_placeholder = np.zeros_like(x_placeholder)
    line, = ax.plot(x_placeholder, y_placeholder, color=palette["spectrum_line"], linewidth=0.8)
    line._normal_color = palette["spectrum_line"]

    # Embed in Tkinter
    canvas = FigureCanvasTkAgg(fig, master=graph_frame)
    canvas.get_tk_widget().configure(bg=palette["canvas_bg"], highlightthickness=0)
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 10))

    # Toolbar
    toolbar = CustomToolbar(canvas, graph_frame, pack_toolbar=False)
    toolbar.update()
    toolbar.pack(side=tk.TOP, anchor=tk.E, padx=(1, 40))

    # Resize handler
    canvas.mpl_connect('resize_event', lambda event: on_resize(event, canvas))

    return graph_frame, fig, ax, canvas, line


def configure_graph_for_device(ax, canvas, line, capabilities):
    """
    Reconfigure axis limits and placeholder data after a spectrometer connects.
    
    Args:
        ax: Matplotlib Axes
        canvas: FigureCanvasTkAgg
        line: The Line2D object created by create_acquisition_graph
        capabilities: A DeviceCapabilities instance from the spectrometer.
    """
    wl_min = capabilities.wavelength_min
    wl_max = capabilities.wavelength_max
    max_int = capabilities.max_intensity
    pixels = capabilities.pixel_count

    ax.set_xlim([wl_min, wl_max])
    ax.set_ylim([0, max_int])

    # Adapt Y-axis label to the device's intensity scale
    if max_int <= 1.0:
        ax.set_ylabel("Intensity (normalised)")
    else:
        ax.set_ylabel("Intensity (counts)")

    # Keep device max available for future display logic.
    ax._device_max_intensity = max_int

    # Reset the line data to match the new pixel count
    x_placeholder = np.linspace(wl_min, wl_max, pixels)
    line.set_xdata(x_placeholder)
    line.set_ydata(np.zeros(pixels))

    ax.set_title(f"Live Spectrum — {capabilities.model}")
    apply_matplotlib_theme(ax.figure, ax, line)
    canvas.draw_idle()


def update_spectrum_fast(ax, canvas, line, wavelengths, intensities):
    """
    Fast spectrum update with hysteresis-based Y-axis auto-scaling.
    
    The Y-axis adapts so small signals (lamp, noise) are clearly visible, but
    only rescales when the non-laser peak moves *significantly* outside the
    current view. The 1064 nm Nd:YAG band stays plotted but does not drive the
    scale. Hysteresis prevents the jittery feel of per-frame auto-scale.
    
    Rescale triggers:
      - Peak > 80% of current Y-top  → zoom out  (signal about to clip)
      - Peak < 25% of current Y-top  → zoom in   (too much wasted space)
    
    New Y-top is set to ``peak × 1.3`` (30 % headroom).  A minimum floor
    prevents pure dark noise from filling the plot.
    
    Args:
        ax: Matplotlib Axes
        canvas: FigureCanvasTkAgg
        line: The Line2D object created by create_acquisition_graph
        wavelengths: np.ndarray of wavelength values
        intensities: np.ndarray of intensity values
    """
    line.set_xdata(wavelengths)
    line.set_ydata(intensities)

    # Update X limits to match actual data range
    if len(wavelengths) > 0:
        ax.set_xlim([wavelengths[0], wavelengths[-1]])

    # --- Hysteresis Y-axis auto-scaling (skipped while the user holds Y) ---
    if len(intensities) > 0 and not getattr(ax, "_hold_y_autoscale", False):
        peak = spectrum_autoscale_peak(wavelengths, intensities)
        current_top = ax.get_ylim()[1]

        # Minimum floor keeps dark noise visible without letting it fill the view.
        min_ylim = LIVE_SPECTRUM_MIN_Y_TOP

        target_top = min_ylim if peak <= min_ylim else peak * 1.3
        needs_rescale = False
        if peak > current_top * 0.80:
            # Signal approaching the top — zoom out
            needs_rescale = True
        elif current_top > target_top * 1.50:
            # View is much larger than the useful signal range - zoom in.
            needs_rescale = True

        if needs_rescale:
            new_top = target_top
            ax.set_ylim([0, new_top])

    canvas.draw_idle()


def update_title(ax, canvas, title_text):
    """Update the plot title."""
    ax.set_title(title_text)
    canvas.draw_idle()


def _legacy_highlight_captured_spectrum(ax, canvas, wavelengths, intensities, shot_index):
    """
    Briefly show the captured spectrum with a highlight effect.
    Used after a triggered capture to visually confirm the shot.
    """
    # Stash the current title so clear_highlight() can restore it
    ax._pre_capture_title = ax.get_title()

    # Flash the line in a different color
    palette = get_palette()
    highlight_line, = ax.plot(wavelengths, intensities, color=palette["capture_line"], linewidth=1.2, alpha=0.8)
    ax.set_title(f"Captured — Shot #{shot_index}")
    canvas.draw_idle()
    return highlight_line


def _legacy_clear_highlight(ax, canvas, highlight_line):
    """
    Remove the capture highlight line and restore the previous title.
    """
    try:
        highlight_line.remove()
    except (ValueError, AttributeError):
        pass
    # Restore title saved by highlight_captured_spectrum()
    ax.set_title(getattr(ax, '_pre_capture_title', 'Live Spectrum'))
    canvas.draw_idle()


def highlight_captured_spectrum(ax, canvas, live_line, wavelengths, intensities, shot_index):
    """
    Briefly show the captured spectrum with a highlight effect.
    Used after a triggered capture to visually confirm the shot.
    """
    # Keep captured-trigger displays on the same scaling path as live polling.
    _apply_live_spectrum_data(ax, live_line, wavelengths, intensities)

    # Preserve the original live title across rapid repeated captures.
    if not getattr(ax, '_capture_highlight_active', False):
        ax._pre_capture_title = ax.get_title()
    ax._capture_highlight_active = True

    # Reuse the live line so repeated triggers cannot leave stale overlays behind.
    if not hasattr(live_line, '_normal_color'):
        live_line._normal_color = live_line.get_color()
        live_line._normal_linewidth = live_line.get_linewidth()
        live_line._normal_alpha = live_line.get_alpha() if live_line.get_alpha() is not None else 1.0

    palette = get_palette()
    live_line.set_color(palette["capture_line"])
    live_line.set_linewidth(1.2)
    live_line.set_alpha(0.8)
    ax.set_title(f"Captured - Shot #{shot_index}")
    canvas.draw_idle()


def clear_highlight(ax, canvas, live_line):
    """
    Restore the live line style after a capture highlight.
    """
    palette = get_palette()
    live_line.set_color(getattr(live_line, '_normal_color', palette["spectrum_line"]))
    live_line.set_linewidth(getattr(live_line, '_normal_linewidth', 0.8))
    live_line.set_alpha(getattr(live_line, '_normal_alpha', 1.0))
    ax._capture_highlight_active = False
    ax.set_title(getattr(ax, '_pre_capture_title', 'Live Spectrum'))
    canvas.draw_idle()
