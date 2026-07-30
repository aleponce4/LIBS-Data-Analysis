"""Analysis application window and graph/sidebar composition."""

import tkinter as tk
import sv_ttk
from tkinter import StringVar, messagebox, ttk
import sys
import functools
from types import SimpleNamespace
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Rectangle

from prolibspector.analysis.graph import create_graph_space, style_plot_toolbar
from prolibspector.analysis.sidebar import create_sidebar
from prolibspector.core.icons import refresh_themed_icons
from prolibspector.core.inspector import attach_right_inspector
from prolibspector.core.runtime import configure_dpi_awareness, maximize_window, set_window_icon
from prolibspector.core.settings import get_ui_settings
from prolibspector.core.ui_scale import apply_text_scale
from prolibspector.core.tooltip import attach_tooltip
from prolibspector.core.ui_theme import apply_canvas_theme, apply_matplotlib_theme, get_palette
from prolibspector.core.windowing import apply_window_policy


def _mapping_color_limits(values, mode: str, manual_min=None, manual_max=None):
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    mode_key = str(mode or "Auto p95").strip().lower()
    if mode_key == "manual":
        return manual_min, manual_max
    if finite.size == 0:
        return 0.0, None
    if mode_key == "max":
        vmax = float(np.nanmax(finite))
    else:
        vmax = float(np.nanpercentile(finite, 95))
    vmin = 0.0
    if np.isfinite(vmax) and vmax > vmin:
        return vmin, vmax
    return vmin, None



def _state_winner_grid(point_table, state, shape, row_positions, column_positions, element_to_code, skip_grid=None):
    """Grid of element codes: per cell, the highest-confidence element in `state`."""
    grid = np.full(shape, np.nan, dtype=float)
    subset = point_table[point_table["state"] == state]
    if subset.empty:
        return grid
    winners = subset.sort_values("confidence", ascending=False, kind="stable").drop_duplicates(
        ["row_index", "column_index"],
        keep="first",
    )
    for row in winners.itertuples(index=False):
        row_pos = row_positions.get(int(row.row_index))
        column_pos = column_positions.get(int(row.column_index))
        code = element_to_code.get(str(row.element_symbol))
        if row_pos is None or column_pos is None or code is None:
            continue
        if skip_grid is not None and np.isfinite(skip_grid[row_pos, column_pos]):
            continue
        grid[row_pos, column_pos] = float(code)
    return grid


def _finish_map_axes(axis, result, title_suffix):
    axis.set_xlim(float(result.x_edges[0]), float(result.x_edges[-1]))
    axis.set_ylim(float(result.y_edges[0]), float(result.y_edges[-1]))
    axis.set_xlabel("X position (mm)")
    axis.set_ylabel("Y position (mm)")
    axis.set_title(f"{result.run_data.sample_label} - {title_suffix}")
    axis.set_aspect("equal", adjustable="box")


def _draw_categorical_overview(axis, result, palette, solid_grid, muted_grid, title_suffix):
    """Shared renderer for element-code winner grids (confidence overview)."""
    elements = list(result.config.element_symbols)
    x_edges = np.asarray(result.x_edges, dtype=float)
    y_edges = np.asarray(result.y_edges, dtype=float)
    fallback_color = palette.get("muted_text", "#666666")
    cmap = ListedColormap([result.element_colors.get(element, fallback_color) for element in elements] or [fallback_color])
    cmap.set_bad(palette["axes_bg"])
    norm = BoundaryNorm(np.arange(0.5, len(elements) + 1.5, 1.0), cmap.N)
    if muted_grid is not None and np.isfinite(muted_grid).any():
        axis.pcolormesh(
            x_edges,
            y_edges,
            np.ma.masked_invalid(muted_grid),
            cmap=cmap,
            norm=norm,
            shading="auto",
            edgecolors="none",
            linewidth=0.0,
            alpha=0.35,
        )
    axis.pcolormesh(
        x_edges,
        y_edges,
        np.ma.masked_invalid(solid_grid),
        cmap=cmap,
        norm=norm,
        shading="auto",
        edgecolors="none",
        linewidth=0.0,
    )
    _finish_map_axes(axis, result, title_suffix)


def _rgb_composite_image(result, channel_elements):
    """H x W x 3 image: one element per RGB channel, each scaled to its own p95.

    Mixed colors mean co-located elements (e.g. red + green -> yellow); an
    absent channel (None) stays black.
    """
    shape = (len(result.run_data.row_indices), len(result.run_data.column_indices))
    image = np.zeros(shape + (3,), dtype=float)
    grids = getattr(result, "element_intensity_grids", {}) or {}
    for channel, element in enumerate(channel_elements[:3]):
        if not element:
            continue
        grid = grids.get(element)
        if grid is None:
            continue
        values = np.asarray(grid, dtype=float)
        if values.shape != shape:
            continue
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            continue
        # p05-p95 contrast stretch per channel: without the floor subtraction a
        # map's base offset fills the whole channel and the composite washes out.
        low = float(np.nanpercentile(finite, 5))
        high = float(np.nanpercentile(finite, 95))
        if not np.isfinite(high) or high <= low:
            continue
        stretched = (np.nan_to_num(values, nan=low) - low) / (high - low)
        image[..., channel] = np.clip(stretched, 0.0, 1.0)
    return image


_ION_ROMAN = {1: "I", 2: "II", 3: "III"}


def _line_status_word(row) -> str:
    """One decision-oriented word per candidate line for the evidence table."""
    reason = str(getattr(row, "candidate_reason", "") or "")
    if reason == "atmospheric_overlap":
        return "air"
    if not bool(getattr(row, "line_usable", True)):
        return "weak"
    if bool(getattr(row, "overlaps_selected_element", False)):
        return "used·ovl"
    if float(getattr(row, "overlap_penalty", 0.0) or 0.0) > 0.05:
        return "used·ovl"
    return "used"


def _display_median_smooth(grid):
    """3x3 nan-safe median for map display only (raw values stay untouched).

    Single-shot maps carry full per-pulse variance; a small spatial median
    shows the underlying structure without altering any stored quantity.
    """
    values = np.asarray(grid, dtype=float)
    if values.ndim != 2 or min(values.shape) < 3:
        return values
    try:
        from scipy.ndimage import median_filter
    except Exception:
        return values
    mask = ~np.isfinite(values)
    filled = np.where(mask, np.nanmedian(values) if np.isfinite(values).any() else 0.0, values)
    smoothed = median_filter(filled, size=3, mode="nearest")
    smoothed[mask] = np.nan
    return smoothed


def _element_legend_items(result, elements):
    colors = getattr(result, "element_colors", {}) or {}
    fallback_color = get_palette()["muted_text"]
    return [
        {"label": element, "color": colors.get(element, fallback_color), "element": element}
        for element in elements
    ]


class App:
    # Initialize the main window
    def __init__(self, root=None):
        configure_dpi_awareness()

        #Fix duplicate graph space?
        self.current_graph_space = None

        self.theme_name = "sun-valley"  # Store the theme name as an instance variable

        if root is not None:
            # Reuse the shared application root (no new Tk interpreter)
            self.root = root
        else:
            # Standalone launch (e.g. from __main__ block)
            self.root = tk.Tk()
            self.root.withdraw()
            try:
                sv_ttk.set_theme(get_ui_settings()["theme"].lower())
            except Exception:
                pass
        apply_text_scale(self.root)

        self.root.title("LIBS Identification Software")
        set_window_icon(self.root)
        apply_window_policy(
            self.root,
            preferred=(1680, 980),
            min_size=(1100, 700),
            max_fraction=(0.94, 0.90),
            resizable=True,
        )

        # Initialize mode_var after self.root is created
        self.mode_var = tk.StringVar(value="Analysis")

        # Add this line to initialize peak_values
        self.peak_labels = []
        self.peak_values = []
        self.arrows = []
        self.line = None

        # Set default values for threshold_percent and round_off_error_tolerance
        self.current_threshold_percent = 10
        self.round_off_error_tolerance = 0

        # Add this line to initialize round_off_error_tolerance_var
        self.round_off_error_tolerance_var = tk.IntVar()

        # Set normalized flag
        self.normalized = False

        # Intitilize empty element list
        self.element_df = None

        # Initialize peak_data for csv export
        self.peak_data = []
        self.data = pd.DataFrame()
        self.replicate_data = pd.DataFrame()
        self.x_data = pd.Series(dtype=float)
        self.y_data = pd.Series(dtype=float)
        self._original_y_data = None
        self.analysis_status_var = tk.StringVar(value="No spectrum loaded.")
        self.data_action_buttons = []
        self.mapping_action_buttons = []
        self.mapping_result_action_buttons = []
        self.mapping_update_buttons = []
        self.analysis_workspace_var = tk.StringVar(value="spectrum")
        self.mapping_status_var = tk.StringVar(value="No mapping folder loaded.")
        self.mapping_run_data = None
        self.mapping_result = None
        self.mapping_selected_elements = []
        self.mapping_colorbar = None
        self.mapping_analysis_busy = False
        self._mapping_hover_cid = None
        self._requested_mode = None


        # AnalysisApp only renders the analysis graph surface.
        self.current_graph_space, self.fig, self.ax = create_graph_space(self)
        self._mapping_spectrum_context = None
        self._mapping_spectrum_click_cid = None
        if hasattr(self, "mapping_evidence_tree"):
            self.mapping_evidence_tree.bind("<<TreeviewSelect>>", self._on_mapping_evidence_select)

        # Create the menu bar
        create_sidebar(self)
        self.inspector = attach_right_inspector(
            self.root,
            self,
            mode_name="Analysis",
            status_var=self.analysis_status_var,
            switch_label="Open Acquisition",
        )
        self.apply_ui_theme()

        # Initialize spectrometer attribute
        self.spectrometer = None

        # Initialize selected_element
        self.selected_element = StringVar()

        # Bind the closing event to the custom function
        self.root.protocol("WM_DELETE_WINDOW", functools.partial(self.on_closing))


    # Run the main loop
    def run(self):
        self._reveal_for_run()
        self.root.mainloop()

    def _reveal_for_run(self):
        """Reveal the fully-built analysis window in one visible step."""
        alpha_changed = False
        original_alpha = 1.0
        try:
            try:
                original_alpha = self.root.wm_attributes("-alpha")
            except (tk.TclError, TypeError, AttributeError):
                original_alpha = 1.0
            try:
                self.root.wm_attributes("-alpha", 0.01)
                alpha_changed = True
            except (tk.TclError, TypeError, AttributeError):
                alpha_changed = False
            self.root.update_idletasks()
            self.root.deiconify()
            maximize_window(self.root)
            self.root.update_idletasks()
            try:
                self.root.update()
            except tk.TclError:
                pass
        finally:
            if alpha_changed:
                try:
                    self.root.wm_attributes("-alpha", original_alpha)
                except (tk.TclError, TypeError, AttributeError):
                    pass

    def has_spectrum(self):
        """Return True when the analysis surface has spectrum data loaded."""
        if self.line is None:
            return False
        return hasattr(self, "x_data") and hasattr(self.x_data, "__len__") and len(self.x_data) > 0

    def has_mapping_run(self):
        return getattr(self, "mapping_run_data", None) is not None

    def has_mapping_result(self):
        return getattr(self, "mapping_result", None) is not None

    def set_analysis_status(self, text):
        """Update the persistent Analysis mode status strip if it exists."""
        if hasattr(self, "analysis_status_var"):
            self.analysis_status_var.set(text)

    def refresh_data_action_state(self):
        """Enable or disable sidebar actions that require a loaded spectrum."""
        state = "normal" if self.has_spectrum() else "disabled"
        for button in getattr(self, "data_action_buttons", []):
            try:
                button.config(state=state)
            except tk.TclError:
                pass
        mapping_busy = bool(getattr(self, "mapping_analysis_busy", False))
        mapping_state = "normal" if self.has_mapping_run() and not mapping_busy else "disabled"
        mapping_result_state = "normal" if self.has_mapping_result() and not mapping_busy else "disabled"
        mapping_update_state = (
            "normal"
            if self.has_mapping_run() and len(getattr(self, "mapping_selected_elements", [])) >= 1 and not mapping_busy
            else "disabled"
        )
        for button in getattr(self, "mapping_action_buttons", []):
            try:
                button.config(state=mapping_state)
            except tk.TclError:
                pass
        for button in getattr(self, "mapping_result_action_buttons", []):
            try:
                button.config(state=mapping_result_state)
            except tk.TclError:
                pass
        for button in getattr(self, "mapping_update_buttons", []):
            try:
                button.config(state=mapping_update_state)
            except tk.TclError:
                pass
        cache_state = "normal" if self.has_mapping_run() and not mapping_busy else "disabled"
        for button in getattr(self, "mapping_cache_buttons", []):
            try:
                button.config(state=cache_state)
            except tk.TclError:
                pass
        if hasattr(self, "mapping_cancel_button"):
            try:
                self.mapping_cancel_button.config(state="normal" if mapping_busy else "disabled")
            except tk.TclError:
                pass

    def _clear_peak_annotations(self):
        for collection in (self.peak_labels, self.arrows):
            for item in list(collection):
                try:
                    item.remove()
                except (AttributeError, ValueError):
                    pass
        self.peak_labels = []
        self.peak_values = []
        self.arrows = []
        self.peak_data = []
        self.element_df = None

    def _remove_mapping_colorbar(self):
        colorbar = getattr(self, "mapping_colorbar", None)
        if colorbar is not None:
            try:
                colorbar.remove()
            except (AttributeError, ValueError, tk.TclError):
                pass
        self.mapping_colorbar = None

    def _disconnect_mapping_hover(self):
        cid = getattr(self, "_mapping_hover_cid", None)
        if cid is not None and hasattr(self, "canvas"):
            try:
                self.canvas.mpl_disconnect(cid)
            except Exception:
                pass
        self._mapping_hover_cid = None

    def set_loaded_spectrum(
        self,
        x_data,
        y_data,
        *,
        data=None,
        replicate_data=None,
        title="Loaded Spectrum",
        status_text=None,
    ):
        """Load spectrum data into the analysis plot and refresh dependent UI."""
        self.x_data = x_data
        self.y_data = y_data
        self.mapping_result = None
        self._disconnect_mapping_hover()
        self._remove_mapping_colorbar()
        self._clear_mapping_legend()
        self._clear_mapping_evidence_table()
        if data is not None:
            self.data = data
        if replicate_data is not None:
            self.replicate_data = replicate_data
        self._original_y_data = None
        self._clear_peak_annotations()

        self._reset_plot_axis()
        palette = get_palette()
        self.ax.plot(self.x_data, self.y_data, color=palette["spectrum_line"])
        self.line = self.ax.lines[-1]

        x_min, x_max = self.x_data.min(), self.x_data.max()
        margin = (x_max - x_min) * 0.02 if x_max != x_min else 1.0
        self.ax.set_xlim([x_min - margin, x_max + margin])
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Relative Intensity")
        self.ax.set_title(title)
        apply_matplotlib_theme(self.fig, self.ax, self.line, palette)
        self.canvas.draw()

        point_count = len(self.x_data) if hasattr(self.x_data, "__len__") else 0
        self.set_analysis_status(status_text or f"Loaded {title} ({point_count} points).")
        if hasattr(self, "mapping_multi_summary_var"):
            try:
                self.mapping_multi_summary_var.set("No multi-element result.")
            except tk.TclError:
                pass
        self.refresh_data_action_state()

    def clear_spectrum(self, *, confirm=False):
        """Clear the analysis plot and reset data-dependent UI."""
        if confirm and (self.has_spectrum() or self.peak_labels or self.peak_data):
            if not messagebox.askyesno(
                "Clean Plot",
                "This will clear the current spectrum, labels, and peak data.\n\nContinue?",
                parent=self.root,
            ):
                return False

        self._clear_peak_annotations()
        self.line = None
        self.mapping_result = None
        self._disconnect_mapping_hover()
        self._remove_mapping_colorbar()
        self._clear_mapping_legend()
        self._clear_mapping_evidence_table()
        self.data = pd.DataFrame()
        self.replicate_data = pd.DataFrame()
        self.x_data = pd.Series(dtype=float)
        self.y_data = pd.Series(dtype=float)
        self._original_y_data = None
        self.normalized = False
        self._reset_plot_axis()
        self.ax.set_xlim([100, 1000])
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Relative Intensity")
        apply_matplotlib_theme(self.fig, self.ax)
        self.canvas.draw()
        self.set_analysis_status("No spectrum loaded.")
        if hasattr(self, "mapping_multi_summary_var"):
            try:
                self.mapping_multi_summary_var.set("No multi-element result.")
            except tk.TclError:
                pass
        self.refresh_data_action_state()
        return True

    def clear_mapping_plot(self, *, status_text=None):
        """Clear mapping render state without dropping the loaded mapping run."""
        self.mapping_result = None
        self._disconnect_mapping_hover()
        self._remove_mapping_colorbar()
        self._clear_mapping_legend()
        self._clear_mapping_evidence_table()
        self._clear_peak_annotations()
        self.line = None
        self._reset_plot_axis()
        self.ax.set_xlabel("X position (mm)")
        self.ax.set_ylabel("Y position (mm)")
        self.ax.set_title("2D Mapping")
        apply_matplotlib_theme(self.fig, self.ax)
        self.canvas.draw()
        if hasattr(self, "mapping_multi_summary_var"):
            try:
                self.mapping_multi_summary_var.set("No multi-element result.")
            except tk.TclError:
                pass
        if status_text:
            self.set_analysis_status(status_text)
        self.refresh_data_action_state()

    def _style_mapping_colorbar(self, palette):
        colorbar = getattr(self, "mapping_colorbar", None)
        if colorbar is None:
            return
        try:
            colorbar.ax.yaxis.label.set_color(palette["text"])
            colorbar.ax.tick_params(colors=palette["text"])
            colorbar.outline.set_edgecolor(palette["spine"])
        except Exception:
            pass

    def _reset_plot_axis(self):
        """Restore the figure to one primary axes after multi-panel views."""
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        self._mapping_data_axes = [self.ax]
        self.mapping_colorbar = None

    def _mapping_multi_view(self):
        try:
            value = str(self.mapping_multi_view_var.get() or "Small Multiples")
        except Exception:
            value = "Small Multiples"
        value_key = value.strip().lower()
        if value_key.startswith("element"):
            return "detail"
        if value_key.startswith("rgb"):
            return "rgb"
        if value_key.startswith("confidence"):
            return "confidence"
        return "small_multiples"

    def _selected_mapping_detail_element(self, result):
        elements = list(result.config.element_symbols)
        if not elements:
            return ""
        try:
            requested = str(self.mapping_detail_element_var.get() or "").strip()
        except Exception:
            requested = ""
        if requested in elements:
            return requested
        return elements[0]

    def _connect_multi_mapping_hover(self, result):
        self._disconnect_mapping_hover()
        x_coords = np.asarray(result.x_coords, dtype=float)
        y_coords = np.asarray(result.y_coords, dtype=float)
        if x_coords.size == 0 or y_coords.size == 0:
            return
        data_axes = set(getattr(self, "_mapping_data_axes", [self.ax]))

        def _on_motion(event):
            if event.inaxes not in data_axes or event.xdata is None or event.ydata is None:
                return
            column_pos = int(np.nanargmin(np.abs(x_coords - float(event.xdata))))
            row_pos = int(np.nanargmin(np.abs(y_coords - float(event.ydata))))
            row_index = int(result.run_data.row_indices[row_pos])
            column_index = int(result.run_data.column_indices[column_pos])
            subset = result.point_element_table[
                (result.point_element_table["row_index"] == row_index)
                & (result.point_element_table["column_index"] == column_index)
            ].copy()
            if subset.empty:
                return
            subset = subset.sort_values("confidence", ascending=False, kind="stable")
            detected = subset[subset["state"] == "detected"]
            uncertain = subset[subset["state"] == "uncertain"]
            detected_text = ", ".join(
                f"{row.element_symbol} {float(row.confidence):.2f}" for row in detected.itertuples(index=False)
            ) or "none"
            uncertain_text = ", ".join(
                f"{row.element_symbol} {float(row.confidence):.2f}" for row in uncertain.itertuples(index=False)
            ) or "none"
            best = subset.iloc[0]
            readout = (
                f"R{row_index:03d} C{column_index:03d} · X {float(best['x_mm']):.2f} Y {float(best['y_mm']):.2f} mm\n"
                f"Detected: {detected_text} · Uncertain: {uncertain_text}\n"
                f"Best line: {best['element_symbol']} {best['best_wavelength']:.3f} nm, "
                f"SNR {float(best['best_snr']):.1f}, support {int(best['supporting_lines'])}"
            )
            hover_var = getattr(self, "mapping_hover_var", None)
            if hover_var is not None:
                hover_var.set(readout)
            else:
                self.set_analysis_status(readout.replace("\n", " | "))

        self._mapping_hover_cid = self.canvas.mpl_connect("motion_notify_event", _on_motion)

    def _refresh_mapping_right_rail(self):
        panel = getattr(self, "mapping_right_rail", None) or getattr(self, "mapping_evidence_panel", None)
        if panel is None:
            return
        visible = bool(getattr(self, "_mapping_legend_visible", False) or getattr(self, "_mapping_evidence_visible", False))
        paned = getattr(self, "mapping_body_paned", None)
        try:
            if paned is not None:
                managed = str(panel) in paned.panes()
                if visible and not managed:
                    paned.add(panel, weight=0)
                elif not visible and managed:
                    paned.forget(panel)
            elif visible:
                panel.grid()
            else:
                panel.grid_remove()
        except tk.TclError:
            pass

    def _open_mapping_element_detail(self, element):
        """Jump to an element's detail view (legend chip click-through)."""
        result = getattr(self, "mapping_result", None)
        if result is None or not getattr(result, "is_multi_element", False):
            return
        try:
            self.mapping_detail_element_var.set(str(element))
            self.mapping_multi_view_var.set("Element Detail")
        except (tk.TclError, AttributeError):
            return
        try:
            from prolibspector.analysis.sidebar import _refresh_mapping_display_controls

            _refresh_mapping_display_controls(self)
        except Exception:
            pass
        self.render_mapping_result(result)

    def _set_mapping_legend(self, items=(), *, title="Legend", note=""):
        frame = getattr(self, "mapping_legend_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        palette = get_palette()
        title_var = getattr(self, "mapping_legend_title_var", None)
        if title_var is not None:
            try:
                title_var.set(str(title or "Legend"))
            except tk.TclError:
                pass
        row_index = 0
        for item in items:
            color = str(item.get("color", palette["muted_text"]))
            label = str(item.get("label", ""))
            element = str(item.get("element", "") or "")
            row = ttk.Frame(frame)
            row.grid(row=row_index, column=0, sticky="ew", pady=1)
            row.columnconfigure(1, weight=1)
            swatch = tk.Frame(
                row,
                width=16,
                height=14,
                bg=color,
                highlightthickness=1,
                highlightbackground=palette["spine"],
            )
            swatch.grid(row=0, column=0, sticky="w", padx=(0, 6))
            swatch.grid_propagate(False)
            text_label = ttk.Label(row, text=label)
            text_label.grid(row=0, column=1, sticky="w")
            if element:
                # Legend chips double as navigation: click -> element detail.
                callback = functools.partial(self._open_mapping_element_detail, element)
                for widget in (row, swatch, text_label):
                    widget.bind("<Button-1>", lambda _event, cb=callback: cb())
                    try:
                        widget.configure(cursor="hand2")
                    except tk.TclError:
                        pass
                attach_tooltip(text_label, f"Click to open the {element} element detail view.")
            row_index += 1
        if note:
            ttk.Label(
                frame,
                text=str(note),
                style="AnalysisStatus.TLabel",
                justify="left",
                wraplength=330,
            ).grid(row=row_index, column=0, sticky="ew", pady=(6, 0))
            row_index += 1
        self._mapping_legend_visible = bool(items or note)
        self._refresh_mapping_right_rail()

    def _clear_mapping_legend(self):
        self._set_mapping_legend(())

    @staticmethod
    def _evidence_sort_key(value):
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (1, str(value).lower())

    def _set_mapping_evidence_table(self, columns, rows):
        tree = getattr(self, "mapping_evidence_tree", None)
        if tree is None:
            return
        if not columns:
            try:
                tree.delete(*tree.get_children())
            except tk.TclError:
                pass
            self._mapping_evidence_visible = False
            self._refresh_mapping_right_rail()
            return
        self._mapping_evidence_columns = list(columns)
        self._mapping_evidence_rows = list(rows)
        self._mapping_evidence_sort = (None, False)
        try:
            self._populate_mapping_evidence_tree()
            self._mapping_evidence_visible = True
            self._refresh_mapping_right_rail()
        except tk.TclError:
            pass

    def _populate_mapping_evidence_tree(self):
        tree = self.mapping_evidence_tree
        columns = self._mapping_evidence_columns
        rows = list(self._mapping_evidence_rows)
        sort_column, sort_reverse = getattr(self, "_mapping_evidence_sort", (None, False))
        if sort_column is not None:
            rows.sort(key=lambda row: self._evidence_sort_key(row.get(sort_column, "")), reverse=sort_reverse)
        palette = get_palette()
        tree.delete(*tree.get_children())
        column_ids = [str(column[0]) for column in columns]
        tree.configure(columns=column_ids)
        # Numeric columns read best right-aligned; detect from the data.
        numeric = {
            column_id: bool(rows)
            and all(self._evidence_sort_key(row.get(column_id, ""))[0] == 0 for row in rows if row.get(column_id, "") != "")
            for column_id in column_ids
        }
        for column_id, heading, width in columns:
            marker = ""
            if column_id == sort_column:
                marker = " ▼" if sort_reverse else " ▲"
            tree.heading(
                str(column_id),
                text=f"{heading}{marker}",
                command=functools.partial(self._sort_mapping_evidence, str(column_id)),
            )
            tree.column(str(column_id), width=int(width), anchor="e" if numeric.get(column_id) else "w", stretch=False)
        tree.tag_configure("odd", background=palette["axes_bg"])
        for index, row in enumerate(rows):
            tree.insert(
                "",
                "end",
                values=[row.get(column_id, "") for column_id in column_ids],
                tags=("odd",) if index % 2 else (),
            )

    def _sort_mapping_evidence(self, column_id):
        current_column, current_reverse = getattr(self, "_mapping_evidence_sort", (None, False))
        # First click sorts descending (largest z/SNR first is the useful order).
        reverse = not current_reverse if current_column == column_id else True
        self._mapping_evidence_sort = (column_id, reverse)
        try:
            self._populate_mapping_evidence_tree()
        except tk.TclError:
            pass

    def _clear_mapping_evidence_table(self):
        self._set_mapping_evidence_table((), ())
        self._update_mapping_spectrum_inset(None, None)

    def _mapping_intensity_limits(self, values):
        finite = np.asarray(values, dtype=float)
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return 0.0, None
        vmin = float(np.nanpercentile(finite, 5))
        vmax = float(np.nanpercentile(finite, 95))
        if np.isfinite(vmin) and np.isfinite(vmax) and vmax > vmin:
            return vmin, vmax
        vmax = float(np.nanmax(finite))
        if np.isfinite(vmax) and vmax > 0:
            return 0.0, vmax
        return 0.0, None

    def _mapping_mean_spectrum(self, result):
        """Run-average spectrum (calibrated axis), memoized per result."""
        cache = getattr(self, "_mapping_mean_spectrum_cache", None)
        if cache is not None and cache.get("ref") is result:
            return cache["wavelengths"], cache["mean"]
        from prolibspector.analysis.mapping_analysis import load_or_build_processed_mapping_run

        processed = load_or_build_processed_mapping_run(result.run_data, result.preprocess_config)
        intensities = processed.intensities
        total = np.zeros(intensities.shape[1], dtype=np.float64)
        chunk = 512
        for start in range(0, intensities.shape[0], chunk):
            total += np.asarray(intensities[start : start + chunk, :], dtype=np.float64).sum(axis=0)
        mean = total / max(intensities.shape[0], 1)
        wavelengths = np.asarray(processed.wavelengths, dtype=float)
        self._mapping_mean_spectrum_cache = {"ref": result, "wavelengths": wavelengths, "mean": mean}
        return wavelengths, mean

    def _update_mapping_spectrum_inset(self, result, element, zoom_nm=None):
        frame = getattr(self, "mapping_spectrum_frame", None)
        ax = getattr(self, "mapping_spectrum_ax", None)
        if frame is None or ax is None:
            return
        if result is None or not element:
            try:
                frame.grid_remove()
            except tk.TclError:
                pass
            self._mapping_spectrum_context = None
            return
        try:
            wavelengths, mean = self._mapping_mean_spectrum(result)
        except Exception as exc:
            try:
                frame.grid_remove()
            except tk.TclError:
                pass
            self.set_analysis_status(f"Spectrum inset unavailable ({exc}).")
            return
        palette = get_palette()
        evidence = getattr(result, "line_evidence_table", pd.DataFrame())
        lines = pd.DataFrame()
        if evidence is not None and not evidence.empty:
            lines = evidence[evidence["element_symbol"].astype(str) == str(element)][
                ["wavelength", "line_usable"]
            ].drop_duplicates(subset=["wavelength"])
        ax.clear()
        if zoom_nm is not None:
            window = (wavelengths >= zoom_nm - 4.0) & (wavelengths <= zoom_nm + 4.0)
            ax.plot(wavelengths[window], mean[window], lw=1.0, color=palette["text"])
            ax.set_xlim(zoom_nm - 4.0, zoom_nm + 4.0)
            title = f"{element} · {zoom_nm:.2f} nm (click plot for full range)"
        else:
            ax.plot(wavelengths, mean, lw=0.5, color=palette["text"])
            title = f"Average spectrum · {element} lines (select a row to zoom)"
        for row in lines.itertuples(index=False):
            usable = bool(getattr(row, "line_usable", False))
            ax.axvline(
                float(row.wavelength),
                color=palette["qc_pass"] if usable else palette["muted_text"],
                lw=1.1 if usable else 0.7,
                ls="-" if usable else ":",
                alpha=0.9 if usable else 0.6,
            )
        ax.set_title(title, fontsize=8, color=palette["text"])
        ax.tick_params(labelsize=7)
        ax.set_yticks([])
        ax.margins(x=0.01)
        self.mapping_spectrum_fig.subplots_adjust(left=0.04, right=0.98, top=0.82, bottom=0.18)
        apply_matplotlib_theme(self.mapping_spectrum_fig, ax, palette=palette)
        self._mapping_spectrum_context = (result, str(element))
        try:
            frame.grid()
        except tk.TclError:
            pass
        self.mapping_spectrum_canvas.draw_idle()
        if getattr(self, "_mapping_spectrum_click_cid", None) is None:
            self._mapping_spectrum_click_cid = self.mapping_spectrum_canvas.mpl_connect(
                "button_press_event", self._on_mapping_spectrum_click
            )

    def _on_mapping_spectrum_click(self, _event):
        context = getattr(self, "_mapping_spectrum_context", None)
        if context:
            self._update_mapping_spectrum_inset(context[0], context[1], zoom_nm=None)

    def _on_mapping_evidence_select(self, _event=None):
        context = getattr(self, "_mapping_spectrum_context", None)
        tree = getattr(self, "mapping_evidence_tree", None)
        if not context or tree is None:
            return
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0]).get("values", ())
        try:
            zoom_nm = float(values[0])
        except (TypeError, ValueError, IndexError):
            return
        self._update_mapping_spectrum_inset(context[0], context[1], zoom_nm=zoom_nm)

    def _update_multi_mapping_evidence_table(self, result, *, element=None):
        if element:
            table = getattr(result, "line_evidence_table", pd.DataFrame())
            if table is None or table.empty:
                self._clear_mapping_evidence_table()
                return
            subset = table[table["element_symbol"].astype(str) == str(element)].copy()
            if subset.empty:
                self._clear_mapping_evidence_table()
                return
            group_columns = [
                column
                for column in (
                    "wavelength",
                    "ionization_level",
                    "candidate_reason",
                    "overlap_penalty",
                    "overlaps_selected_element",
                    "line_usable",
                )
                if column in subset.columns
            ]
            grouped = (
                subset.groupby(group_columns, dropna=False)
                .agg(
                    p95_z=("quality_z", lambda values: values.quantile(0.95)),
                    p95_snr=("local_snr", lambda values: values.quantile(0.95)),
                )
                .reset_index()
                .sort_values(["p95_z", "p95_snr"], ascending=False, kind="stable")
            )
            rows = []
            for row in grouped.head(14).itertuples(index=False):
                rows.append(
                    {
                        "line": f"{float(row.wavelength):.2f}",
                        "ion": _ION_ROMAN.get(int(row.ionization_level), str(int(row.ionization_level))),
                        "z": f"{float(row.p95_z):.1f}",
                        "snr": f"{float(row.p95_snr):.1f}",
                        "status": _line_status_word(row),
                    }
                )
            self._set_mapping_evidence_table(
                (
                    ("line", "Line nm", 82),
                    ("ion", "Ion", 40),
                    ("z", "z", 58),
                    ("snr", "SNR", 58),
                    ("status", "Status", 96),
                ),
                rows,
            )
            inset = getattr(self, "_update_mapping_spectrum_inset", None)
            if callable(inset):
                inset(result, element)
            return

        inset = getattr(self, "_update_mapping_spectrum_inset", None)
        if callable(inset):
            inset(None, None)
        rows = []
        intensity_grids = getattr(result, "element_intensity_grids", {}) or {}
        for row in result.summary_table.itertuples(index=False):
            element = str(row.element)
            grid = np.asarray(intensity_grids.get(element, []), dtype=float)
            finite = grid[np.isfinite(grid)]
            rows.append(
                {
                    "element": element,
                    "max_intensity": f"{float(np.nanmax(finite)):.3g}" if finite.size else "",
                    "median_intensity": f"{float(np.nanmedian(finite)):.3g}" if finite.size else "",
                    "detected": int(row.detected_points),
                    "uncertain": int(row.uncertain_points),
                    "lines": int(row.lines_used),
                }
            )
        self._set_mapping_evidence_table(
            (
                ("element", "El", 44),
                ("max_intensity", "Max Int", 72),
                ("median_intensity", "Med Int", 72),
                ("detected", "Det", 48),
                ("uncertain", "Unc", 50),
                ("lines", "Lines", 52),
            ),
            rows,
        )

    def _render_multi_element_small_multiples(self, result, palette):
        elements = list(result.config.element_symbols)
        if not elements:
            self.ax.set_title("No elements selected")
            return (), "Small Multiples", ""
        count = len(elements)
        # Choose the grid from the figure's aspect ratio so square map panels
        # tile the canvas edge to edge instead of leaving horizontal gaps.
        fig_w, fig_h = self.fig.get_size_inches()
        fig_aspect = max(float(fig_w) / max(float(fig_h), 1e-6), 0.1)
        columns = min(6, max(1, int(round(np.sqrt(count * fig_aspect)))))
        columns = min(columns, count)
        rows = int(np.ceil(count / columns))
        self.fig.clear()
        axes = self.fig.subplots(rows, columns, squeeze=False, sharex=True, sharey=True)
        axes_flat = list(axes.ravel())
        self._mapping_data_axes = []
        cmap_name = getattr(self, "mapping_palette_var", None).get() if hasattr(self, "mapping_palette_var") else "viridis"
        diagnostics = getattr(result, "diagnostics", {})
        usable_counts = diagnostics.get("usable_line_counts", {}) if isinstance(diagnostics, dict) else {}
        denoise_method = getattr(self, "_mapping_display_denoise", None)
        denoise = denoise_method() if callable(denoise_method) else False
        shared_var = getattr(self, "mapping_shared_scale_var", None)
        try:
            shared_scale = bool(shared_var.get()) if shared_var is not None else False
        except Exception:
            shared_scale = False
        shared_limits = None
        if shared_scale:
            pooled = np.concatenate([
                np.asarray(grid, dtype=float)[np.isfinite(np.asarray(grid, dtype=float))]
                for grid in (getattr(result, "element_intensity_grids", {}) or {}).values()
            ] or [np.asarray([], dtype=float)])
            if pooled.size:
                shared_limits = (float(np.nanpercentile(pooled, 5)), float(np.nanpercentile(pooled, 95)))
        last_mesh = None
        for index, element in enumerate(elements):
            axis = axes_flat[index]
            self._mapping_data_axes.append(axis)
            grid = getattr(result, "element_intensity_grids", {}).get(element)
            values = np.asarray(grid, dtype=float) if grid is not None else np.full(
                (len(result.run_data.row_indices), len(result.run_data.column_indices)),
                np.nan,
                dtype=float,
            )
            if denoise:
                values = _display_median_smooth(values)
            usable_lines = int(usable_counts.get(element, 0) or 0)
            has_signal = usable_lines > 0 and np.isfinite(values).any()
            if shared_limits is not None:
                vmin, vmax = shared_limits
            else:
                limit_method = getattr(self, "_mapping_intensity_limits", None)
                vmin, vmax = limit_method(values) if callable(limit_method) else (0.0, None)
            last_mesh = axis.pcolormesh(
                result.x_edges,
                result.y_edges,
                np.ma.masked_invalid(values),
                cmap=cmap_name,
                shading="auto",
                vmin=vmin,
                vmax=vmax,
                alpha=1.0 if has_signal else 0.3,
            )
            plural = "s" if usable_lines != 1 else ""
            label = f"{element} · {usable_lines} line{plural}" if has_signal else f"{element} · no usable lines"
            # In-panel label instead of a title row: panels sit edge to edge
            # with shared axes, so every pixel of figure height goes to maps.
            axis.text(
                0.03,
                0.97,
                label,
                transform=axis.transAxes,
                va="top",
                ha="left",
                fontsize=9,
                color="#ffffff" if has_signal else "#c9c9c9",
                bbox=dict(facecolor="#000000", alpha=0.45, edgecolor="none", pad=2.0),
            )
            axis.set_aspect("equal", adjustable="box")
            axis.set_xlim(float(result.x_edges[0]), float(result.x_edges[-1]))
            axis.set_ylim(float(result.y_edges[0]), float(result.y_edges[-1]))
            axis.tick_params(labelsize=8)
            if index // columns == rows - 1:
                axis.set_xlabel("X mm", fontsize=9)
            if index % columns == 0:
                axis.set_ylabel("Y mm", fontsize=9)
        for axis in axes_flat[count:]:
            axis.set_visible(False)
        self.ax = axes_flat[0]
        self.fig.suptitle(f"{result.run_data.sample_label} - multi-element intensity maps")
        self.fig.subplots_adjust(left=0.05, right=0.985, top=0.93, bottom=0.07, wspace=0.04, hspace=0.05)
        if shared_limits is not None and last_mesh is not None:
            self.mapping_colorbar = self.fig.colorbar(
                last_mesh, ax=self._mapping_data_axes, fraction=0.025, pad=0.02
            )
            self.mapping_colorbar.set_label("Net line intensity (shared scale)")
            note = (
                "All panels share one p05-p95 intensity scale - colors are comparable "
                "across elements. Dimmed panels have no usable lines."
            )
        else:
            note = (
                "Each panel is contrast-stretched to its own p05-p95 net line intensity - "
                "colors are not comparable across panels. Dimmed panels have no usable lines."
            )
        # No color swatches: every panel uses the same intensity colormap, so
        # element colors would describe nothing on screen.
        return (), "Small Multiples", note

    def _mapping_rgb_channels(self, result):
        """Resolve the R/G/B channel elements: user picks, else the three most
        spatially structured intensity maps."""
        elements = list(result.config.element_symbols)
        picks = []
        for name in ("mapping_rgb_r_var", "mapping_rgb_g_var", "mapping_rgb_b_var"):
            var = getattr(self, name, None)
            try:
                value = str(var.get()).strip() if var is not None else ""
            except Exception:
                value = ""
            picks.append(value if value in elements else "")
        if any(picks):
            return picks
        from prolibspector.analysis.mapping_analysis import neighbor_correlation

        grids = getattr(result, "element_intensity_grids", {}) or {}
        coherence = {
            element: neighbor_correlation(grids[element])
            for element in elements
            if element in grids
        }
        scored = sorted(
            coherence,
            key=lambda element: -(coherence[element] if np.isfinite(coherence[element]) else -1.0),
        )
        auto = (scored + ["", "", ""])[:3]
        for name, value in zip(("mapping_rgb_r_var", "mapping_rgb_g_var", "mapping_rgb_b_var"), auto):
            var = getattr(self, name, None)
            if var is not None and value:
                try:
                    var.set(value)
                except tk.TclError:
                    pass
        return auto

    def _render_multi_element_rgb(self, result, palette):
        channels = self._mapping_rgb_channels(result)
        source = result
        denoise_method = getattr(self, "_mapping_display_denoise", None)
        if callable(denoise_method) and denoise_method():
            grids = {
                element: _display_median_smooth(grid)
                for element, grid in (getattr(result, "element_intensity_grids", {}) or {}).items()
            }
            source = SimpleNamespace(run_data=result.run_data, element_intensity_grids=grids)
        image = _rgb_composite_image(source, channels)
        x_edges = np.asarray(result.x_edges, dtype=float)
        y_edges = np.asarray(result.y_edges, dtype=float)
        self.ax.imshow(
            image,
            extent=(float(x_edges[0]), float(x_edges[-1]), float(y_edges[0]), float(y_edges[-1])),
            origin="lower",
            interpolation="nearest",
            aspect="equal",
        )
        _finish_map_axes(self.ax, result, "RGB composite")
        channel_colors = ("#E5484D", "#46A758", "#3E63DD")
        items = [
            {"label": f"{name}: {element}", "color": color, "element": element}
            for name, color, element in zip(("R", "G", "B"), channel_colors, channels)
            if element
        ]
        return (
            items,
            "RGB Composite",
            "Each channel is one element's net intensity scaled to its own p95. "
            "Mixed colors mean co-located elements (R+G = yellow).",
        )

    def _render_confidence_overview(self, result, palette):
        elements = list(result.config.element_symbols)
        element_to_code = {element: index + 1 for index, element in enumerate(elements)}
        shape = (len(result.run_data.row_indices), len(result.run_data.column_indices))
        row_positions = {int(row): pos for pos, row in enumerate(result.run_data.row_indices)}
        column_positions = {int(column): pos for pos, column in enumerate(result.run_data.column_indices)}
        detected_grid = _state_winner_grid(
            result.point_element_table, "detected", shape, row_positions, column_positions, element_to_code
        )
        uncertain_grid = _state_winner_grid(
            result.point_element_table,
            "uncertain",
            shape,
            row_positions,
            column_positions,
            element_to_code,
            skip_grid=detected_grid,
        )
        title_suffix = "confidence overview"
        if not (np.isfinite(detected_grid).any() or np.isfinite(uncertain_grid).any()):
            message = (
                "No cells reach the detected/uncertain thresholds at this preset.\n"
                "Switch to an intensity view, or set the Detection preset to Loose."
            )
            self.ax.text(
                0.5,
                0.5,
                message,
                transform=self.ax.transAxes,
                ha="center",
                va="center",
                fontsize=11,
                color=palette["text"],
                wrap=True,
            )
            _finish_map_axes(self.ax, result, title_suffix)
            return (), "Confidence Overview", "No detected or uncertain cells in this result."
        _draw_categorical_overview(self.ax, result, palette, detected_grid, uncertain_grid, title_suffix)
        return (
            _element_legend_items(result, elements),
            "Confidence Overview",
            "Solid cells are detected; muted cells are uncertain.",
        )

    def _render_multi_element_detail(self, result, palette, element):
        quantity_var = getattr(self, "mapping_detail_quantity_var", None)
        try:
            quantity = str(quantity_var.get()) if quantity_var is not None else "Intensity"
        except Exception:
            quantity = "Intensity"
        intensity_grid = getattr(result, "element_intensity_grids", {}).get(element)
        use_intensity = False
        if quantity == "Intensity" and intensity_grid is not None:
            raw_grid = np.asarray(intensity_grid, dtype=float)
            use_intensity = bool(np.isfinite(raw_grid).any())
        if use_intensity:
            title_suffix = "intensity map"
            pulse = self._mapping_selected_pulse()
            if pulse is not None:
                evidence = result.line_evidence_table
                lines = evidence[
                    (evidence["element_symbol"] == element) & evidence["line_usable"].astype(bool)
                ].drop_duplicates(subset=["line_id"])
                try:
                    pulse_grid = self._mapping_pulse_grid(result, lines, ("detail", element))
                except Exception as exc:
                    pulse_grid = None
                    self._mapping_pulse_warning = f"Pulse view unavailable ({exc}); showing all-pulse aggregate."
                if pulse_grid is not None and np.isfinite(pulse_grid).any():
                    raw_grid = np.asarray(pulse_grid, dtype=float)
                    title_suffix = f"intensity map - pulse {pulse}"
            denoise_method = getattr(self, "_mapping_display_denoise", None)
            if callable(denoise_method) and denoise_method():
                raw_grid = _display_median_smooth(raw_grid)
            values = np.ma.masked_invalid(raw_grid)
            colorbar_label = f"{element} net line intensity (p05-p95 display)"
            limit_method = getattr(self, "_mapping_intensity_limits", None)
            vmin, vmax = limit_method(raw_grid) if callable(limit_method) else (0.0, None)
        else:
            values = np.ma.masked_invalid(np.asarray(result.element_grids[element], dtype=float))
            colorbar_label = f"{element} spectral evidence confidence"
            title_suffix = "confidence map"
            vmin, vmax = 0.0, 1.0
        mesh = self.ax.pcolormesh(
            result.x_edges,
            result.y_edges,
            values,
            cmap=getattr(self, "mapping_palette_var", None).get() if hasattr(self, "mapping_palette_var") else "viridis",
            shading="auto",
            vmin=vmin,
            vmax=vmax,
        )
        self.mapping_colorbar = self.fig.colorbar(mesh, ax=self.ax)
        self.mapping_colorbar.set_label(colorbar_label)
        subset = result.point_element_table[result.point_element_table["element_symbol"] == element]
        detected = subset[subset["state"] == "detected"]
        if not detected.empty:
            self.ax.scatter(
                detected["x_mm"],
                detected["y_mm"],
                marker="s",
                s=90,
                facecolors="none",
                edgecolors="white",
                linewidths=1.5,
                label="Detected",
            )
        _finish_map_axes(self.ax, result, f"{element} {title_suffix}")
        return (
            _element_legend_items(result, [element]),
            "Element Detail",
            f"Map value: {quantity}. White outline on the plot marks confidence-detected points.",
        )

    def _mapping_show_missing_overlay(self) -> bool:
        var = getattr(self, "mapping_show_missing_var", None)
        try:
            return bool(var.get()) if var is not None else True
        except Exception:
            return True

    def _mapping_show_contours(self) -> bool:
        var = getattr(self, "mapping_show_contours_var", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _apply_mapping_axis_flips(self):
        """Invert map axis direction to match the physical sample orientation.

        Coordinates stay in true stage millimeters; only the viewing direction
        changes (some rigs move an axis opposite the plotting convention).
        """
        def _flag(name):
            var = getattr(self, name, None)
            try:
                return bool(var.get()) if var is not None else False
            except Exception:
                return False

        for axis in getattr(self, "_mapping_data_axes", [self.ax]):
            if _flag("mapping_flip_x_var") and not axis.xaxis_inverted():
                axis.invert_xaxis()
            if _flag("mapping_flip_y_var") and not axis.yaxis_inverted():
                axis.invert_yaxis()

    def _mapping_display_denoise(self) -> bool:
        var = getattr(self, "mapping_denoise_var", None)
        try:
            return bool(var.get()) if var is not None else False
        except Exception:
            return False

    def _mapping_selected_pulse(self):
        """Depth-slider position: None for the all-pulse aggregate, else 1..n."""
        var = getattr(self, "mapping_pulse_var", None)
        try:
            value = int(round(float(var.get()))) if var is not None else 0
        except Exception:
            value = 0
        return value if value >= 1 else None

    def _mapping_pulse_slider_applies(self, result) -> bool:
        """The depth slider drives intensity views only; confidence views
        always show the full-aggregate detection result."""
        if not getattr(result, "is_multi_element", False):
            return True
        if self._mapping_multi_view() != "detail":
            return False
        var = getattr(self, "mapping_detail_quantity_var", None)
        try:
            return str(var.get()) == "Intensity" if var is not None else False
        except Exception:
            return False

    def _update_mapping_pulse_slider(self, result):
        """Fit the depth slider range to the loaded run's shots per point."""
        scale = getattr(self, "mapping_pulse_scale", None)
        if scale is None:
            return
        try:
            max_pulse = int(pd.to_numeric(result.run_data.index["shot_number"], errors="coerce").max())
        except Exception:
            max_pulse = 1
        max_pulse = max(1, max_pulse)
        applies = self._mapping_pulse_slider_applies(result)
        try:
            scale.configure(to=float(max_pulse), state="normal" if (max_pulse > 1 and applies) else "disabled")
            if self._mapping_selected_pulse() is not None and self._mapping_selected_pulse() > max_pulse:
                self.mapping_pulse_var.set(0)
        except tk.TclError:
            pass
        self._refresh_mapping_pulse_label(applies=applies)

    def _refresh_mapping_pulse_label(self, applies: bool = True):
        label_var = getattr(self, "mapping_pulse_label_var", None)
        if label_var is None:
            return
        pulse = self._mapping_selected_pulse()
        try:
            if not applies:
                label_var.set("int. only")
            else:
                label_var.set("Average" if pulse is None else f"Pulse {pulse}")
        except tk.TclError:
            pass

    def _mapping_method_summary(self, result) -> str:
        """Compact provenance line, e.g. 'v6 · air-ref · cal ✓ · 4 lines · Normal'."""
        version = str(result.diagnostics.get("detection_method_version", ""))
        version = version.replace("multi-element-targeted-", "") or "?"
        pre = result.preprocess_config
        norm = {
            "Air-plasma reference": "air-ref",
            "Total Intensity": "TIC",
            "Min-Max": "min-max",
            "None": "raw",
        }.get(str(pre.normalization), str(pre.normalization))
        calibration = "cal ✓" if getattr(pre, "wavelength_calibration_enabled", False) else "cal ✗"
        config = getattr(result, "config", None) or getattr(result, "element_config", None)
        parts = [version, norm, calibration]
        if config is not None:
            parts.append(f"{int(getattr(config, 'max_candidate_lines', 0))} lines")
            preset = getattr(config, "preset", "")
            if preset:
                parts.append(str(preset))
        return " · ".join(parts)

    def _mapping_pulse_grid(self, result, lines, scope):
        """Compute (and memoize) the per-pulse intensity grid for the current view."""
        pulse = self._mapping_selected_pulse()
        if pulse is None or lines is None or getattr(lines, "empty", True):
            return None
        cache = getattr(self, "_mapping_pulse_grid_cache", None)
        if cache is None or cache.get("_result_ref") is not result:
            cache = {"_result_ref": result}
            self._mapping_pulse_grid_cache = cache
        key = (scope, pulse)
        if key not in cache:
            from prolibspector.analysis.mapping_analysis import compute_pulse_element_intensity_grid

            config = getattr(result, "element_config", None) or getattr(result, "config", None)
            peak_window = float(getattr(config, "peak_window_nm", 0.2) or 0.2)
            cache[key] = compute_pulse_element_intensity_grid(
                result.run_data,
                result.preprocess_config,
                lines,
                pulse,
                peak_window_nm=peak_window,
                aggregate_method=str(result.preprocess_config.aggregate_shots or "median"),
            )
        return cache[key]

    def _overlay_single_mapping_missing(self, result, values, palette):
        if not self._mapping_show_missing_overlay():
            return
        missing = ~np.isfinite(np.asarray(values, dtype=float))
        if not np.any(missing):
            return
        x_edges = np.asarray(result.x_edges, dtype=float)
        y_edges = np.asarray(result.y_edges, dtype=float)
        for row_pos, column_pos in np.argwhere(missing):
            self.ax.add_patch(
                Rectangle(
                    (float(x_edges[column_pos]), float(y_edges[row_pos])),
                    float(x_edges[column_pos + 1] - x_edges[column_pos]),
                    float(y_edges[row_pos + 1] - y_edges[row_pos]),
                    facecolor="#8A8F94",
                    edgecolor=palette["spine"],
                    linewidth=0.5,
                    hatch="//",
                    alpha=0.55,
                    label="_missing",
                )
            )

    def _overlay_single_mapping_contours(self, result, values, palette):
        if not self._mapping_show_contours():
            return
        numeric = np.asarray(values, dtype=float)
        finite = numeric[np.isfinite(numeric)]
        if finite.size < 4:
            return
        threshold = float(np.nanpercentile(finite, 90))
        if not np.isfinite(threshold) or threshold <= float(np.nanmin(finite)):
            return
        try:
            self.ax.contour(
                np.asarray(result.x_coords, dtype=float),
                np.asarray(result.y_coords, dtype=float),
                numeric,
                levels=[threshold],
                colors=[palette["text"]],
                linewidths=0.9,
                alpha=0.75,
            )
        except Exception:
            pass

    def render_multi_element_result(self, result):
        self.mapping_result = result
        self._remove_mapping_colorbar()
        self._clear_peak_annotations()
        self.line = None
        self._reset_plot_axis()
        palette = get_palette()
        self._mapping_pulse_warning = ""
        self._update_mapping_pulse_slider(result)
        view = self._mapping_multi_view()
        if view == "detail":
            element = self._selected_mapping_detail_element(result)
            if hasattr(self, "mapping_detail_element_var") and element:
                try:
                    self.mapping_detail_element_var.set(element)
                except tk.TclError:
                    pass
            legend_spec = self._render_multi_element_detail(result, palette, element)
            self._update_multi_mapping_evidence_table(result, element=element)
            detail = f"{element} detail"
        elif view == "rgb":
            legend_spec = self._render_multi_element_rgb(result, palette)
            self._update_multi_mapping_evidence_table(result)
            detail = "RGB composite"
        elif view == "confidence":
            legend_spec = self._render_confidence_overview(result, palette)
            self._update_multi_mapping_evidence_table(result)
            detail = "confidence overview"
        else:
            legend_spec = self._render_multi_element_small_multiples(result, palette)
            self._update_multi_mapping_evidence_table(result)
            detail = "small multiples"
        # Views return (items, title, note); flips, theme, hover and legend are
        # applied here, once, so no view can drift out of step with the others.
        legend_items, legend_title, legend_note = legend_spec or ((), "Legend", "")
        self._set_mapping_legend(legend_items, title=legend_title, note=legend_note)
        self._apply_mapping_axis_flips()
        for axis in getattr(self, "_mapping_data_axes", [self.ax]):
            apply_matplotlib_theme(self.fig, axis, palette=palette)
        self._style_mapping_colorbar(palette)
        self._connect_multi_mapping_hover(result)
        self.canvas.draw()

        detected_total = int((result.point_element_table["state"] == "detected").sum())
        uncertain_total = int((result.point_element_table["state"] == "uncertain").sum())
        self.set_analysis_status(
            f"Rendered multi-element {detail}: {len(result.config.element_symbols)} element(s), "
            f"{detected_total} detection(s), {uncertain_total} uncertain. "
            f"[{self._mapping_method_summary(result)}]"
            + (f" {self._mapping_pulse_warning}" if getattr(self, "_mapping_pulse_warning", "") else "")
        )
        self.refresh_data_action_state()

    def render_mapping_result(self, result):
        """Render a 2D mapping result in the main Analysis plot space."""
        if getattr(result, "is_multi_element", False):
            self.render_multi_element_result(result)
            return
        self.mapping_result = result
        self._mapping_pulse_warning = ""
        self._disconnect_mapping_hover()
        self._remove_mapping_colorbar()
        self._clear_peak_annotations()
        self.line = None
        self._reset_plot_axis()

        palette = get_palette()
        self._update_mapping_pulse_slider(result)
        display_grid = np.asarray(result.value_grid, dtype=float)
        denoise_method = getattr(self, "_mapping_display_denoise", None)
        if callable(denoise_method) and denoise_method():
            display_grid = _display_median_smooth(display_grid)
        title_suffix = "map"
        pulse = self._mapping_selected_pulse()
        if pulse is not None:
            if "accepted" in result.line_scores.columns:
                accepted_lines = result.line_scores[result.line_scores["accepted"].astype(bool)]
            else:
                accepted_lines = result.line_scores
            try:
                pulse_grid = self._mapping_pulse_grid(result, accepted_lines, ("single", result.element_label))
            except Exception as exc:
                pulse_grid = None
                self._mapping_pulse_warning = f"Pulse view unavailable ({exc}); showing all-pulse aggregate."
            if pulse_grid is not None and np.isfinite(pulse_grid).any():
                display_grid = np.asarray(pulse_grid, dtype=float)
                title_suffix = f"map - pulse {pulse}"
        values = np.ma.masked_invalid(display_grid)
        self._clear_mapping_legend()
        self._clear_mapping_evidence_table()
        def _optional_float(var, fallback):
            if var is None:
                return fallback
            try:
                text = str(var.get()).strip()
            except (tk.TclError, AttributeError):
                return fallback
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return fallback

        manual_min = _optional_float(getattr(self, "mapping_color_min_var", None), result.element_config.color_min)
        manual_max = _optional_float(getattr(self, "mapping_color_max_var", None), result.element_config.color_max)
        try:
            color_scale_mode = str(self.mapping_color_scale_var.get() or "Auto p95")
        except Exception:
            color_scale_mode = "Auto p95"
        vmin, vmax = _mapping_color_limits(display_grid, color_scale_mode, manual_min, manual_max)
        mesh = self.ax.pcolormesh(
            result.x_edges,
            result.y_edges,
            values,
            cmap=getattr(self, "mapping_palette_var", None).get() if hasattr(self, "mapping_palette_var") else result.element_config.colormap,
            shading="auto",
            vmin=vmin,
            vmax=vmax,
        )
        self._overlay_single_mapping_missing(result, display_grid, palette)
        self._overlay_single_mapping_contours(result, display_grid, palette)
        self.mapping_colorbar = self.fig.colorbar(mesh, ax=self.ax)
        self.mapping_colorbar.set_label(f"Normalized {result.element_label} intensity")
        self.ax.set_xlabel("X position (mm)")
        self.ax.set_ylabel("Y position (mm)")
        self.ax.set_title(f"{result.run_data.sample_label} - {result.element_label} {title_suffix}")
        self.ax.set_aspect("equal", adjustable="box")
        self._apply_mapping_axis_flips()
        apply_matplotlib_theme(self.fig, self.ax, palette=palette)
        self._style_mapping_colorbar(palette)
        self.canvas.draw()

        missing = result.diagnostics.get("spectra_missing", 0)
        accepted = result.diagnostics.get("accepted_line_count", result.accepted_line_count)
        self.set_analysis_status(
            f"Rendered {result.element_label} map: {len(result.point_table)} point(s), "
            f"{accepted} accepted line(s), {missing} missing spectrum file(s). "
            f"[{self._mapping_method_summary(result)}]"
            + (f" {self._mapping_pulse_warning}" if getattr(self, "_mapping_pulse_warning", "") else "")
        )
        if hasattr(self, "mapping_multi_summary_var"):
            try:
                self.mapping_multi_summary_var.set("No multi-element result.")
            except tk.TclError:
                pass
        self.refresh_data_action_state()

    def apply_ui_theme(self):
        """Apply the saved UI theme to Analysis canvases and plot surfaces."""
        apply_text_scale(self.root)
        palette = get_palette()
        style = ttk.Style()
        style.configure("Emphasized.TLabel", foreground=palette["text"])
        style.configure("AnalysisStatus.TLabel", foreground=palette["muted_text"])
        style.configure("Muted.TLabel", foreground=palette["muted_text"])
        graph_frame = getattr(self, "graph_frame", None) or getattr(self, "current_graph_space", None)
        if graph_frame is not None:
            graph_frame.configure(bg=palette["canvas_bg"])
        if hasattr(self, "canvas"):
            try:
                self.canvas.get_tk_widget().configure(bg=palette["canvas_bg"])
            except tk.TclError:
                pass
        if hasattr(self.root, "sidebar_canvas"):
            apply_canvas_theme(self.root.sidebar_canvas, palette)
        if hasattr(self, "fig") and hasattr(self, "ax"):
            apply_matplotlib_theme(self.fig, self.ax, self.line, palette)
            self._style_mapping_colorbar(palette)
        toolbar = getattr(self, "plot_toolbar", None)
        if toolbar is not None:
            style_plot_toolbar(toolbar, palette)
            if hasattr(self, "canvas"):
                self.canvas.draw_idle()
        refresh_themed_icons(self.root)

    def get_status_snapshot(self):
        if getattr(self, "analysis_workspace_var", None) is not None and self.analysis_workspace_var.get() == "mapping":
            if self.has_mapping_result():
                result = self.mapping_result
                if getattr(result, "is_multi_element", False):
                    detected_total = int((result.point_element_table["state"] == "detected").sum())
                    uncertain_total = int((result.point_element_table["state"] == "uncertain").sum())
                    summary = (
                        f"Multi-element map loaded\nElements: {len(result.config.element_symbols)}\n"
                        f"Detected cells: {detected_total}\nUncertain cells: {uncertain_total}"
                    )
                else:
                    summary = (
                        f"2D map loaded\nElement: {result.element_label}\n"
                        f"Points: {len(result.point_table)}\nAccepted lines: {result.accepted_line_count}"
                    )
            elif self.has_mapping_run():
                run_data = self.mapping_run_data
                summary = (
                    f"Mapping run loaded\n{run_data.grid_rows} x {run_data.grid_columns} grid\n"
                    f"Spectra: {run_data.resolved_spectra}/{run_data.shots_total}"
                )
            else:
                summary = "No mapping folder loaded"
        elif self.has_spectrum():
            point_count = len(self.x_data) if hasattr(self.x_data, "__len__") else 0
            label_count = len(self.peak_labels)
            peak_count = len(self.peak_data)
            summary = f"Spectrum loaded\nPoints: {point_count}\nLabels: {label_count}\nMatched peaks: {peak_count}"
        else:
            summary = "No spectrum loaded"
        return {"mode": "Analysis", "summary": summary}

    def get_requested_mode(self):
        return self._requested_mode

    def request_mode_switch(self, target_mode):
        from prolibspector.app.launcher import MODE_CHOOSER

        if target_mode not in ("Acquisition", MODE_CHOOSER):
            return
        if self.has_spectrum() or self.peak_labels or self.peak_data or self.has_mapping_run() or self.has_mapping_result():
            destination = "Acquisition" if target_mode == "Acquisition" else "the mode menu"
            if not messagebox.askyesno(
                "Switch Mode",
                f"This will close the current Analysis workspace and open {destination} without sending data.\n\nContinue?",
                parent=self.root,
            ):
                return
        self._requested_mode = target_mode
        self._cleanup_and_quit()

    def _cleanup_and_quit(self):
        poll_after_id = getattr(self, "mapping_analysis_poll_after_id", None)
        if poll_after_id is not None:
            self.mapping_analysis_poll_after_id = None
            try:
                self.root.after_cancel(poll_after_id)
            except tk.TclError:
                pass
        cancel_event = getattr(self, "mapping_analysis_cancel_event", None)
        if cancel_event is not None:
            try:
                cancel_event.set()
            except Exception:
                pass
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        for widget in self.root.winfo_children():
            widget.destroy()
        self.root.quit()

    # Add the on_closing method inside the App class
    def on_closing(self):
        # Stop the mapping poll/worker before tearing the window down so the
        # background thread doesn't outlive the Tk root.
        poll_after_id = getattr(self, "mapping_analysis_poll_after_id", None)
        if poll_after_id is not None:
            self.mapping_analysis_poll_after_id = None
            try:
                self.root.after_cancel(poll_after_id)
            except tk.TclError:
                pass
        cancel_event = getattr(self, "mapping_analysis_cancel_event", None)
        if cancel_event is not None:
            try:
                cancel_event.set()
            except Exception:
                pass

        # Close the tkinter window
        self.root.destroy()

        # Terminate the program
        sys.exit(0)


# Run the app
if __name__ == "__main__":
    app = App()
    app.run()
