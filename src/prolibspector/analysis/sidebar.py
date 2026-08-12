"""Sidebar UI for analysis mode."""

import functools
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from prolibspector.analysis.element_database import DEFAULT_DATABASE_LABEL, database_option_names
from prolibspector.analysis.import_dialog import open_import_dialog
from prolibspector.analysis.mapping_analysis import (
    MAPPING_PALETTES,
    MULTI_ELEMENT_DETECTION_PRESETS,
    ElementMapConfig,
    MappingPreprocessConfig,
    MultiElementMapConfig,
    build_element_map,
    build_multi_element_map,
    clear_mapping_analysis_cache,
    load_mapping_run,
    mapping_analysis_cache_summary,
    validate_background_reference,
)
from prolibspector.analysis.presets_dialog import open_presets_dialog
from prolibspector.analysis.element_table import MAX_SELECTED_ELEMENTS, build_periodic_grid
from prolibspector.analysis.search_element import periodic_table_window
from prolibspector.analysis.workflows import (
    add_to_training_library,
    adjust_plot,
    adjust_spectrum,
    apply_calibration_curve_lazy,
    clean_plot,
    export_data,
    export_mapping_data,
    export_mapping_spectra_csvs,
    export_plot,
)
from prolibspector.core.icons import configure_themed_icon
from prolibspector.core.tooltip import attach_tooltip
from prolibspector.acquisition.sidebar import _install_mousewheel_scrolling, _shield_wheel_value_changes
from prolibspector.core.ui_scale import scaled_font, scaled_padding, ui_scale_for_widget
from prolibspector.core.ui_theme import apply_canvas_theme, get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog, make_scrollable_frame

ANALYSIS_SIDEBAR_WIDTH = 560
MAPPING_WRAP_LENGTH = 490
ANALYSIS_BUTTON_ICON_SIZE = (32, 32)
ANALYSIS_BUTTON_PAD_Y = 5


def _configure_icon(widget, icon_name: str, size: tuple[int, int]) -> None:
    try:
        configure_themed_icon(widget, icon_name, size)
    except Exception:
        pass


def _float_from_var(var, default: float) -> float:
    try:
        return float(var.get())
    except (tk.TclError, TypeError, ValueError):
        return default


def _int_from_var(var, default: int) -> int:
    try:
        return int(float(var.get()))
    except (tk.TclError, TypeError, ValueError):
        return default


def _mapping_preprocess_config_from_sidebar(app) -> MappingPreprocessConfig:
    workers_text = str(app.mapping_parallel_workers_var.get() or "Auto").strip()
    parallel_workers = 0 if workers_text.lower() == "auto" else _int_from_var(app.mapping_parallel_workers_var, 0)
    return MappingPreprocessConfig(
        background_enabled=bool(app.mapping_background_enabled_var.get()),
        background_reference_path=(str(app.mapping_background_path_var.get()).strip() or None),
        smoothing_enabled=bool(app.mapping_smoothing_enabled_var.get()),
        smoothing_method=str(app.mapping_smoothing_method_var.get() or "Savitzky-Golay filter"),
        smoothing_strength=max(0, _int_from_var(app.mapping_smoothing_strength_var, 21)),
        normalization=str(app.mapping_normalization_var.get() or "Air-plasma reference"),
        aggregate_shots=str(app.mapping_aggregate_var.get() or "median"),
        wavelength_calibration_enabled=bool(app.mapping_wavelength_cal_var.get()),
        parallel_workers=max(0, parallel_workers),
        chunk_size_spectra=max(1, _int_from_var(app.mapping_chunk_size_var, 2048)),
    )


def _select_mapping_background_reference(app) -> None:
    path = filedialog.askopenfilename(
        title="Select Mapping Background Reference",
        filetypes=[
            ("Spectrum files", "*.csv *.txt *.tsv"),
            ("CSV files", "*.csv"),
            ("Text files", "*.txt *.tsv"),
            ("All files", "*.*"),
        ],
        parent=app.root,
    )
    if not path:
        return
    app.mapping_background_path_var.set(path)
    app.mapping_background_enabled_var.set(True)
    warnings: list[str] = []
    if getattr(app, "mapping_run_data", None) is not None:
        try:
            warnings = validate_background_reference(app.mapping_run_data, path)
        except Exception:
            warnings = []
    if hasattr(app, "set_analysis_status"):
        if warnings:
            app.set_analysis_status("Background reference selected with warnings: " + " ".join(warnings))
        else:
            app.set_analysis_status("Mapping background reference selected. Rebuild analysis to apply it.")


def _clear_mapping_background_reference(app) -> None:
    app.mapping_background_path_var.set("")
    app.mapping_background_enabled_var.set(False)
    if hasattr(app, "set_analysis_status"):
        app.set_analysis_status("Mapping background reference cleared. Rebuild analysis to apply the change.")


def _auto_select_mapping_background(app, run_data) -> None:
    """Preselect the run's own background reference recorded at acquisition time.

    A user-chosen reference is never overwritten — the run-provided one is only
    mentioned. Mismatch warnings are surfaced but do not block preselection.
    """
    reference_info = run_data.manifest.get("background_reference")
    if not isinstance(reference_info, dict):
        return
    reference_name = str(reference_info.get("reference_path") or "").strip()
    if not reference_name:
        return
    reference_path = os.path.join(str(run_data.folder), reference_name)
    if not os.path.isfile(reference_path):
        return
    current = str(app.mapping_background_path_var.get() or "").strip()
    if current and os.path.normcase(os.path.normpath(current)) != os.path.normcase(os.path.normpath(reference_path)):
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status(
                "This mapping run includes its own background reference; keeping your selected file."
            )
        return
    app.mapping_background_path_var.set(reference_path)
    app.mapping_background_enabled_var.set(True)
    warnings = validate_background_reference(run_data, reference_path)
    if hasattr(app, "set_analysis_status"):
        if warnings:
            app.set_analysis_status("Background reference preselected with warnings: " + " ".join(warnings))
        else:
            app.set_analysis_status(
                "Run background reference preselected; it will be subtracted when the analysis is built."
            )


def _mapping_element_config_from_sidebar(app) -> ElementMapConfig:
    selected = tuple(str(symbol).strip() for symbol in getattr(app, "mapping_selected_elements", []) if str(symbol).strip())
    return ElementMapConfig(
        selected_element=selected[0] if len(selected) == 1 else "",
        selected_elements=selected,
        database_label=str(app.mapping_database_var.get() or DEFAULT_DATABASE_LABEL),
        ionization_levels=(
            bool(app.mapping_ion_1_var.get()),
            bool(app.mapping_ion_2_var.get()),
            bool(app.mapping_ion_3_var.get()),
        ),
        tolerance_nm=max(0.01, _float_from_var(app.mapping_tolerance_var, 0.3)),
        peak_window_nm=max(0.01, _float_from_var(app.mapping_peak_window_var, 0.2)),
        colormap=str(app.mapping_palette_var.get() or "viridis"),
        color_min=None,
        color_max=None,
        candidate_pruning_enabled=bool(app.mapping_candidate_pruning_var.get()),
        max_candidate_lines=max(1, _int_from_var(app.mapping_max_candidate_lines_var, 4)),
    )


def _mapping_multi_config_from_sidebar(app) -> MultiElementMapConfig:
    selected = tuple(str(symbol).strip() for symbol in getattr(app, "mapping_selected_elements", []) if str(symbol).strip())
    preset = str(app.mapping_detection_preset_var.get() or "Normal")
    preset_values = MULTI_ELEMENT_DETECTION_PRESETS.get(preset, MULTI_ELEMENT_DETECTION_PRESETS["Normal"])
    return MultiElementMapConfig(
        selected_elements=selected,
        database_label=str(app.mapping_database_var.get() or DEFAULT_DATABASE_LABEL),
        ionization_levels=(
            bool(app.mapping_ion_1_var.get()),
            bool(app.mapping_ion_2_var.get()),
            bool(app.mapping_ion_3_var.get()),
        ),
        tolerance_nm=max(0.01, _float_from_var(app.mapping_tolerance_var, 0.3)),
        peak_window_nm=max(0.01, _float_from_var(app.mapping_peak_window_var, 0.2)),
        detected_threshold=min(0.99, max(0.01, _float_from_var(app.mapping_detected_threshold_var, preset_values["detected_threshold"]))),
        uncertain_threshold=min(0.98, max(0.0, _float_from_var(app.mapping_uncertain_threshold_var, preset_values["uncertain_threshold"]))),
        min_line_z=max(0.0, _float_from_var(app.mapping_min_line_z_var, preset_values["min_line_z"])),
        min_line_snr=max(0.0, _float_from_var(app.mapping_min_line_snr_var, preset_values["min_line_snr"])),
        require_multi_line_agreement=bool(app.mapping_require_multiline_var.get()),
        spatial_smoothing_strength=min(
            1.0,
            max(0.0, _float_from_var(app.mapping_spatial_strength_var, preset_values["spatial_smoothing_strength"])),
        ),
        preset=preset if preset in MULTI_ELEMENT_DETECTION_PRESETS else "Normal",
        view=str(app.mapping_multi_view_var.get() or "Small Multiples"),
        detail_element=str(app.mapping_detail_element_var.get() or ""),
        candidate_pruning_enabled=bool(app.mapping_candidate_pruning_var.get()),
        max_candidate_lines=max(1, _int_from_var(app.mapping_max_candidate_lines_var, 4)),
    )


def _is_multi_mapping_result(result) -> bool:
    return bool(getattr(result, "is_multi_element", False))


def _themed_style(name: str, fallback: str) -> str:
    """Use a Sun Valley style variant when the theme provides it."""
    try:
        return name if ttk.Style().layout(name) else fallback
    except tk.TclError:
        return fallback


def _update_multi_result_controls(app, result=None) -> None:
    result = result or getattr(app, "mapping_result", None)
    elements = []
    if result is not None and _is_multi_mapping_result(result):
        elements = list(result.config.element_symbols)
    elif len(getattr(app, "mapping_selected_elements", [])) > 1:
        elements = list(getattr(app, "mapping_selected_elements", []))
    if hasattr(app, "mapping_detail_element_combo"):
        try:
            app.mapping_detail_element_combo.configure(values=elements)
        except tk.TclError:
            pass
    if elements and hasattr(app, "mapping_detail_element_var") and app.mapping_detail_element_var.get() not in elements:
        app.mapping_detail_element_var.set(elements[0])
    for combo in getattr(app, "mapping_rgb_combos", []):
        try:
            combo.configure(values=[""] + elements)
        except tk.TclError:
            pass
    for name in ("mapping_rgb_r_var", "mapping_rgb_g_var", "mapping_rgb_b_var"):
        var = getattr(app, name, None)
        if var is not None and elements and var.get() and var.get() not in elements:
            var.set("")
    _refresh_mapping_display_controls(app)


def _refresh_mapping_display_controls(app) -> None:
    result = getattr(app, "mapping_result", None)
    selected = list(getattr(app, "mapping_selected_elements", []))
    try:
        map_type = str(app.mapping_map_type_var.get() or "single")
    except Exception:
        map_type = "single"
    multi_active = _is_multi_mapping_result(result) or map_type == "multi" or len(selected) > 1
    try:
        multi_view = str(app.mapping_multi_view_var.get() or "Small Multiples")
    except Exception:
        multi_view = "Small Multiples"
    rgb_active = multi_active and multi_view.lower().startswith("rgb")
    for widget in getattr(app, "mapping_multi_display_widgets", []):
        try:
            _set_grid_visible(widget, multi_active)
        except tk.TclError:
            pass
    for widget in getattr(app, "mapping_single_display_widgets", []):
        try:
            _set_grid_visible(widget, not multi_active)
        except tk.TclError:
            pass
    for widget in getattr(app, "mapping_multi_advanced_widgets", []):
        try:
            _set_grid_visible(widget, multi_active)
        except tk.TclError:
            pass
    for widget in getattr(app, "mapping_rgb_display_widgets", []):
        try:
            _set_grid_visible(widget, rgb_active)
        except tk.TclError:
            pass
    try:
        manual = str(app.mapping_color_scale_var.get() or "Auto p95").lower() == "manual"
    except Exception:
        manual = False
    for widget in getattr(app, "mapping_manual_color_widgets", []):
        try:
            widget.configure(state="normal" if manual and not multi_active else "disabled")
        except tk.TclError:
            pass


def _refresh_multi_summary(app, result=None) -> None:
    result = result or getattr(app, "mapping_result", None)
    if not hasattr(app, "mapping_multi_summary_var"):
        return
    if result is None or not _is_multi_mapping_result(result):
        app.mapping_multi_summary_var.set("No multi-element result.")
        return
    detected_total = int(result.summary_table["detected_points"].sum()) if not result.summary_table.empty else 0
    uncertain_total = int(result.summary_table["uncertain_points"].sum()) if not result.summary_table.empty else 0
    app.mapping_multi_summary_var.set(
        f"{len(result.config.element_symbols)} elements | Detected cells {detected_total} | "
        f"Uncertain cells {uncertain_total}\nSee the plot-side Mapping Evidence table for element and line details."
    )


def _render_existing_mapping_result(app) -> None:
    _refresh_mapping_display_controls(app)
    if app.has_mapping_result():
        app.render_mapping_result(app.mapping_result)


def _cycle_mapping_detail_element(app, step: int) -> None:
    result = getattr(app, "mapping_result", None)
    elements = list(result.config.element_symbols) if result is not None and _is_multi_mapping_result(result) else list(getattr(app, "mapping_selected_elements", []))
    if not elements:
        return
    current = str(app.mapping_detail_element_var.get() or elements[0])
    try:
        index = elements.index(current)
    except ValueError:
        index = 0
    app.mapping_detail_element_var.set(elements[(index + step) % len(elements)])
    app.mapping_multi_view_var.set("Element Detail")
    _render_existing_mapping_result(app)


def _restore_recommended_mapping_settings(app) -> None:
    """Reset every mapping analysis setting to the measured-best defaults."""
    app.mapping_smoothing_enabled_var.set(False)
    app.mapping_smoothing_method_var.set("Savitzky-Golay filter")
    app.mapping_smoothing_strength_var.set("21")
    app.mapping_background_enabled_var.set(False)
    app.mapping_normalization_var.set("Air-plasma reference")
    app.mapping_aggregate_var.set("median")
    app.mapping_wavelength_cal_var.set(True)
    app.mapping_database_var.set(DEFAULT_DATABASE_LABEL)
    # 0.2 nm window: measured against ground truth (see mapping_analysis
    # defaults). Ionization stays at all three - see the note there.
    app.mapping_ion_1_var.set(True)
    app.mapping_ion_2_var.set(True)
    app.mapping_ion_3_var.set(True)
    app.mapping_tolerance_var.set("0.3")
    app.mapping_peak_window_var.set("0.2")
    app.mapping_candidate_pruning_var.set(True)
    app.mapping_max_candidate_lines_var.set("4")
    app.mapping_parallel_workers_var.set("Auto")
    app.mapping_chunk_size_var.set("2048")
    app.mapping_require_multiline_var.set(True)
    app.mapping_flip_x_var.set(True)
    app.mapping_flip_y_var.set(False)
    app.mapping_pulse_var.set(0.0)
    app.mapping_shared_scale_var.set(False)
    app.mapping_denoise_var.set(True)
    if hasattr(app, "_refresh_mapping_pulse_label"):
        app._refresh_mapping_pulse_label()
    app.mapping_detection_preset_var.set("Normal")
    _apply_detection_preset(app)
    app.set_analysis_status("Mapping settings restored to recommended defaults. Update the map to apply.")


def _apply_detection_preset(app) -> None:
    preset = str(app.mapping_detection_preset_var.get() or "Normal")
    values = MULTI_ELEMENT_DETECTION_PRESETS.get(preset, MULTI_ELEMENT_DETECTION_PRESETS["Normal"])
    app.mapping_detected_threshold_var.set(str(values["detected_threshold"]))
    app.mapping_uncertain_threshold_var.set(str(values["uncertain_threshold"]))
    app.mapping_min_line_z_var.set(str(values["min_line_z"]))
    app.mapping_min_line_snr_var.set(str(values["min_line_snr"]))
    app.mapping_spatial_strength_var.set(str(values["spatial_smoothing_strength"]))
    if "require_multi_line_agreement" in values:
        app.mapping_require_multiline_var.set(bool(values["require_multi_line_agreement"]))
    if app.has_mapping_result() and _is_multi_mapping_result(app.mapping_result):
        _apply_mapping_map(app)
    else:
        _refresh_mapping_display_controls(app)


def _set_grid_visible(widget, visible: bool) -> None:
    if visible:
        widget.grid()
    else:
        widget.grid_remove()


def _mapping_summary_text(run_data) -> str:
    if run_data is None:
        return "No mapping folder loaded."
    storage = dict(getattr(run_data, "storage_summary", {}) or {})
    storage_kind = storage.get("storage_kind") or "csv"
    storage_text = "Binary store" if storage_kind == "binary_npy" else ("Binary + CSV" if storage_kind == "mixed" else "CSV files")
    missing_parts = []
    missing_binary = int(storage.get("missing_binary_spectra") or 0)
    missing_csv = int(storage.get("missing_csv_spectra") or 0)
    if missing_binary:
        missing_parts.append(f"binary rows {missing_binary}")
    if missing_csv:
        missing_parts.append(f"CSV files {missing_csv}")
    missing_text = f" | Missing: {', '.join(missing_parts)}" if missing_parts else ""
    return (
        f"{run_data.sample_label}\n"
        f"Grid: {run_data.grid_rows} x {run_data.grid_columns} | Points: {run_data.points_total}\n"
        f"Spectra: {run_data.resolved_spectra}/{run_data.shots_total} | Source: {storage_text}"
        + missing_text
    )


def _refresh_mapping_summary(app, extra: str | None = None) -> None:
    summary = _mapping_summary_text(getattr(app, "mapping_run_data", None))
    if extra:
        summary = f"{summary}\n{extra}"
    if hasattr(app, "mapping_folder_summary_var"):
        app.mapping_folder_summary_var.set(summary)
    if hasattr(app, "mapping_status_var"):
        app.mapping_status_var.set(summary)


def _format_bytes(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} GB"


def _refresh_mapping_cache_status(app) -> None:
    if not hasattr(app, "mapping_cache_status_var"):
        return
    run_data = getattr(app, "mapping_run_data", None)
    summary = mapping_analysis_cache_summary(run_data)
    if not summary.get("exists"):
        app.mapping_cache_status_var.set("Cache: none")
        return
    updated = summary.get("updated_at") or "unknown"
    app.mapping_cache_status_var.set(f"Cache: {_format_bytes(int(summary.get('size_bytes', 0)))} | Updated: {updated}")


def _set_mapping_analysis_busy(app, busy: bool) -> None:
    app.mapping_analysis_busy = bool(busy)
    for button in getattr(app, "mapping_update_buttons", []):
        try:
            button.config(state="disabled" if busy else "normal")
        except tk.TclError:
            pass
    for button in getattr(app, "mapping_cache_buttons", []):
        try:
            button.config(state="disabled" if busy else "normal")
        except tk.TclError:
            pass
    if hasattr(app, "mapping_cancel_button"):
        try:
            app.mapping_cancel_button.config(state="normal" if busy else "disabled")
        except tk.TclError:
            pass
    app.refresh_data_action_state()


def _post_mapping_status(app, text: str) -> None:
    if hasattr(app, "mapping_analysis_progress_var"):
        app.mapping_analysis_progress_var.set(text)
    app.set_analysis_status(text)


def _stage_timing_text(result) -> str:
    timings = dict(getattr(result, "diagnostics", {}).get("stage_timings", {}) or {})
    if not timings:
        return ""
    preferred = [
        ("preprocess_load_or_build_seconds", "pre"),
        ("candidate_and_line_feature_seconds", "features"),
        ("candidate_scan_seconds", "scan"),
        ("evidence_fusion_seconds", "fusion"),
        ("result_cache_load_seconds", "cache load"),
        ("result_cache_write_seconds", "cache write"),
        ("total_build_seconds", "total"),
    ]
    parts = []
    for key, label in preferred:
        value = timings.get(key)
        if value is not None:
            try:
                parts.append(f"{label} {float(value):.1f}s")
            except (TypeError, ValueError):
                pass
    return " | ".join(parts)


def _load_mapping_folder(app) -> None:
    folder = filedialog.askdirectory(title="Select mapping run folder", parent=app.root)
    if not folder:
        return
    try:
        run_data = load_mapping_run(folder)
    except Exception as exc:
        messagebox.showerror("Load Mapping Folder", str(exc), parent=app.root)
        return
    app.mapping_run_data = run_data
    app.mapping_result = None
    _refresh_mapping_summary(app)
    _refresh_mapping_cache_status(app)
    app.clear_mapping_plot(status_text=f"Loaded mapping folder: {run_data.sample_label}")
    try:
        _auto_select_mapping_background(app, run_data)
    except Exception:
        pass
    app.refresh_data_action_state()


def _set_mapping_placeholder(app, text: str) -> None:
    if hasattr(app, "mapping_placeholder_var"):
        app.mapping_placeholder_var.set(text)
    app.clear_mapping_plot(status_text=text)
    app.refresh_data_action_state()


def _apply_mapping_map(app) -> None:
    _start_mapping_analysis_job(app, force_rebuild_cache=False)


def _start_mapping_analysis_job(app, *, force_rebuild_cache: bool = False) -> None:
    selected = tuple(getattr(app, "mapping_selected_elements", []))
    if len(selected) < 1:
        messagebox.showinfo("Select Element", "Select one or more elements to build a mapping view.", parent=app.root)
        return
    if not app.has_mapping_run():
        messagebox.showinfo("Load Mapping Folder", "Load a mapping run folder first.", parent=app.root)
        return
    if getattr(app, "mapping_analysis_busy", False):
        return

    preprocess_config = _mapping_preprocess_config_from_sidebar(app)
    background_warnings: list[str] = []
    if preprocess_config.background_enabled and preprocess_config.background_reference_path:
        try:
            background_warnings = validate_background_reference(
                app.mapping_run_data, preprocess_config.background_reference_path
            )
        except Exception:
            background_warnings = []
    app.mapping_analysis_queue = queue.Queue()
    app.mapping_analysis_cancel_event = threading.Event()
    app.mapping_analysis_job_started_at = time.perf_counter()
    app.mapping_force_rebuild_cache = bool(force_rebuild_cache)
    _set_mapping_analysis_busy(app, True)
    if background_warnings:
        _post_mapping_status(app, "Background warning: " + " ".join(background_warnings))
    _post_mapping_status(app, "Starting mapping analysis...")

    def progress(stage: str, completed: int, total: int, message: str) -> None:
        if app.mapping_analysis_cancel_event.is_set():
            raise RuntimeError("Mapping analysis cancelled.")
        app.mapping_analysis_queue.put(("progress", stage, int(completed), int(total), str(message)))

    # Tk variables (map type + sidebar-derived configs) must be touched on the
    # Tk thread; the worker thread only computes and posts to the queue.
    if len(selected) > 1:
        app.mapping_map_type_var.set("multi")
        config = _mapping_multi_config_from_sidebar(app)
        build_fn = build_multi_element_map
    else:
        app.mapping_map_type_var.set("single")
        config = _mapping_element_config_from_sidebar(app)
        build_fn = build_element_map

    def worker() -> None:
        try:
            result = build_fn(
                app.mapping_run_data,
                preprocess_config,
                config,
                force_rebuild_cache=force_rebuild_cache,
                progress_callback=progress,
            )
            app.mapping_analysis_queue.put(("result", result))
        except Exception as exc:
            app.mapping_analysis_queue.put(("error", str(exc)))

    threading.Thread(target=worker, name="MappingAnalysisWorker", daemon=True).start()
    app.mapping_analysis_poll_after_id = app.root.after(
        100, functools.partial(_poll_mapping_analysis_job, app)
    )


def _poll_mapping_analysis_job(app) -> None:
    app.mapping_analysis_poll_after_id = None
    queue_obj = getattr(app, "mapping_analysis_queue", None)
    if queue_obj is None:
        return
    try:
        while True:
            message = queue_obj.get_nowait()
            kind = message[0]
            if kind == "progress":
                _kind, stage, completed, total, text = message
                elapsed = max(0.0, time.perf_counter() - getattr(app, "mapping_analysis_job_started_at", time.perf_counter()))
                total_text = f"{completed}/{total}" if total else str(completed)
                _post_mapping_status(app, f"{stage}: {total_text} | {elapsed:.1f}s | {text}")
            elif kind == "result":
                _finish_mapping_analysis_job(app, message[1])
                return
            elif kind == "error":
                _set_mapping_analysis_busy(app, False)
                _refresh_mapping_cache_status(app)
                text = str(message[1])
                if "cancelled" in text.lower():
                    _post_mapping_status(app, "Mapping analysis cancelled.")
                else:
                    messagebox.showerror("Build Mapping Analysis", text, parent=app.root)
                    _post_mapping_status(app, f"Mapping analysis failed: {text}")
                return
    except queue.Empty:
        pass
    if getattr(app, "mapping_analysis_busy", False):
        app.mapping_analysis_poll_after_id = app.root.after(
            100, functools.partial(_poll_mapping_analysis_job, app)
        )


def _finish_mapping_analysis_job(app, result) -> None:
    _set_mapping_analysis_busy(app, False)
    _refresh_mapping_cache_status(app)
    if _is_multi_mapping_result(result):
        _update_multi_result_controls(app, result)
        app.render_mapping_result(result)
        _refresh_multi_summary(app, result)
        detected_total = int((result.point_element_table["state"] == "detected").sum())
        uncertain_total = int((result.point_element_table["state"] == "uncertain").sum())
        cache_text = "result cache hit" if result.diagnostics.get("result_cache_hit") else ("preprocess cache hit" if result.diagnostics.get("preprocess_cache_hit") else "cache rebuilt")
        timing_text = _stage_timing_text(result)
        _refresh_mapping_summary(
            app,
            f"Elements: {len(result.config.element_symbols)} | Detected cells: {detected_total} | Uncertain: {uncertain_total} | {cache_text}"
            + (f"\nTiming: {timing_text}" if timing_text else ""),
        )
        _post_mapping_status(app, f"Mapping analysis complete." + (f" {timing_text}" if timing_text else ""))
        _refresh_mapping_display_controls(app)
        return
    app.render_mapping_result(result)
    _refresh_multi_summary(app, None)
    cache_text = "result cache hit" if result.diagnostics.get("result_cache_hit") else ("preprocess cache hit" if result.diagnostics.get("preprocess_cache_hit") else "cache rebuilt")
    timing_text = _stage_timing_text(result)
    _refresh_mapping_summary(
        app,
        f"Element: {result.element_label} | Accepted lines: {result.accepted_line_count} | {cache_text}"
        + (f"\nTiming: {timing_text}" if timing_text else ""),
    )
    _post_mapping_status(app, f"Mapping analysis complete." + (f" {timing_text}" if timing_text else ""))
    _refresh_mapping_display_controls(app)


def _cancel_mapping_analysis_job(app) -> None:
    event = getattr(app, "mapping_analysis_cancel_event", None)
    if event is not None:
        event.set()
    _post_mapping_status(app, "Cancelling mapping analysis...")


def _clear_mapping_cache(app) -> None:
    run_data = getattr(app, "mapping_run_data", None)
    if run_data is None:
        return
    cleared = clear_mapping_analysis_cache(run_data)
    _refresh_mapping_cache_status(app)
    _post_mapping_status(app, f"Cleared mapping analysis cache ({_format_bytes(cleared)}).")


def _rebuild_mapping_cache(app) -> None:
    _start_mapping_analysis_job(app, force_rebuild_cache=True)


def _apply_mapping_map_sync_unused(app) -> None:
    selected = tuple(getattr(app, "mapping_selected_elements", []))
    if len(selected) > 1:
        app.mapping_map_type_var.set("multi")
        try:
            result = build_multi_element_map(
                app.mapping_run_data,
                _mapping_preprocess_config_from_sidebar(app),
                _mapping_multi_config_from_sidebar(app),
            )
        except Exception as exc:
            messagebox.showerror("Build Multi-element Map", str(exc), parent=app.root)
            return
        _update_multi_result_controls(app, result)
        app.render_mapping_result(result)
        _refresh_multi_summary(app, result)
        detected_total = int((result.point_element_table["state"] == "detected").sum())
        uncertain_total = int((result.point_element_table["state"] == "uncertain").sum())
        _refresh_mapping_summary(
            app,
            f"Elements: {len(result.config.element_symbols)} | Detected cells: {detected_total} | Uncertain: {uncertain_total}",
        )
        return
    app.mapping_map_type_var.set("single")
    try:
        result = build_element_map(
            app.mapping_run_data,
            _mapping_preprocess_config_from_sidebar(app),
            _mapping_element_config_from_sidebar(app),
        )
    except Exception as exc:
        messagebox.showerror("Build Element Map", str(exc), parent=app.root)
        return
    app.render_mapping_result(result)
    _refresh_multi_summary(app, None)
    _refresh_mapping_summary(
        app,
        f"Element: {result.element_label} | Accepted lines: {result.accepted_line_count}",
    )


def _open_mapping_element_selector(app) -> None:
    selector = create_dialog(app.root, "Select Mapping Element", resizable=True)
    scale = ui_scale_for_widget(selector)
    selector.rowconfigure(0, weight=1)
    selector.columnconfigure(0, weight=1)
    selected = list(getattr(app, "mapping_selected_elements", []))

    table_scroll, table_frame = make_scrollable_frame(selector, orient="both")
    table_scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    def _on_selection_refused(_element):
        messagebox.showinfo(
            "Selection Limit",
            f"You can only select up to {MAX_SELECTED_ELEMENTS} elements.",
            parent=selector,
        )

    build_periodic_grid(
        table_frame,
        selected=selected,
        scale=scale,
        on_refused=_on_selection_refused,
    )

    footer = ttk.Frame(selector)
    footer.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
    footer.columnconfigure(0, weight=1)

    def apply_selection():
        app.mapping_selected_elements = list(selected)
        if hasattr(app, "mapping_selected_element_var"):
            if len(selected) == 1:
                app.mapping_selected_element_var.set(selected[0])
            elif selected:
                app.mapping_selected_element_var.set(f"{len(selected)} elements selected")
            else:
                app.mapping_selected_element_var.set("No element selected.")
        selector.destroy()
        if len(selected) > 1:
            app.mapping_map_type_var.set("multi")
            _update_multi_result_controls(app)
            if app.has_mapping_run():
                _apply_mapping_map(app)
            else:
                app.set_analysis_status(f"Selected {len(selected)} elements. Load a mapping folder to build the map.")
        elif len(selected) == 1:
            app.mapping_map_type_var.set("single")
            if app.has_mapping_run():
                _apply_mapping_map(app)
            else:
                app.set_analysis_status(f"Selected element {selected[0]}. Load a mapping folder to build the map.")
        _refresh_mapping_display_controls(app)
        app.refresh_data_action_state()

    ttk.Button(footer, text="Cancel", command=selector.destroy).grid(row=0, column=1, padx=5, sticky="e")
    ttk.Button(footer, text="Apply", command=apply_selection).grid(row=0, column=2, padx=5, sticky="e")
    apply_window_policy(
        selector,
        parent=app.root,
        preferred=(1220, 760),
        min_size=(760, 520),
        modal=False,
        resizable=True,
        remember_key="mapping_element_selector",
    )


def create_sidebar(app):
    scale = ui_scale_for_widget(app.root)
    app.data_action_buttons = []
    app.mapping_action_buttons = []
    app.mapping_result_action_buttons = []
    app.mapping_update_buttons = []
    app.mapping_action_buttons = []
    app.root.sidebar_frame = ttk.Frame(app.root, width=ANALYSIS_SIDEBAR_WIDTH)
    app.root.sidebar_frame.grid(row=0, column=0, sticky="ns")
    app.root.sidebar_frame.grid_propagate(False)
    app.root.sidebar_frame.grid_rowconfigure(2, weight=1)
    app.root.sidebar_frame.grid_columnconfigure(0, weight=1)
    app.root.grid_columnconfigure(0, minsize=ANALYSIS_SIDEBAR_WIDTH, weight=0)

    style = ttk.Style()
    palette = get_palette()
    style.configure("Emphasized.TLabel", font=scaled_font(16, "bold", family="lato", scale=scale), foreground=palette["text"])
    app.root.sidebar_label = ttk.Label(app.root.sidebar_frame, text="Menu", style="Emphasized.TLabel")
    app.root.sidebar_label.grid(row=0, column=0, padx=10, pady=(22, 8))
    style.configure("LeftAligned.TButton", anchor="w", padding=scaled_padding(8, 5, scale))
    style.configure("AnalysisStatus.TLabel", font=scaled_font(9, scale=scale), foreground=palette["muted_text"])
    style.configure("Muted.TLabel", font=scaled_font(9, scale=scale), foreground=palette["muted_text"])

    mode_frame = ttk.Frame(app.root.sidebar_frame)
    mode_frame.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
    mode_frame.columnconfigure((0, 1), weight=1)

    scroll_shell = ttk.Frame(app.root.sidebar_frame)
    scroll_shell.grid(row=2, column=0, sticky="nsew", padx=10, pady=2)
    scroll_shell.grid_rowconfigure(0, weight=1)
    scroll_shell.grid_columnconfigure(0, weight=1)

    canvas = tk.Canvas(scroll_shell, highlightthickness=0, borderwidth=0)
    apply_canvas_theme(canvas, palette)
    app.root.sidebar_canvas = canvas
    scrollbar = ttk.Scrollbar(scroll_shell, orient=tk.VERTICAL, command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar_visible = {"value": False}

    content = ttk.Frame(canvas)
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")
    content.columnconfigure(0, weight=1)
    spectrum_content = ttk.Frame(content)
    mapping_content = ttk.Frame(content)
    spectrum_content.grid(row=0, column=0, sticky="ew")
    mapping_content.grid(row=0, column=0, sticky="ew")
    mapping_content.grid_remove()
    spectrum_content.columnconfigure(0, weight=1)
    mapping_content.columnconfigure(0, weight=1)

    def _refresh_scrollregion(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        bbox = canvas.bbox("all")
        needs_scrollbar = bool(bbox) and bbox[3] > canvas.winfo_height()
        if needs_scrollbar and not scrollbar_visible["value"]:
            scrollbar.grid(row=0, column=1, sticky="ns")
            scrollbar_visible["value"] = True
        elif not needs_scrollbar and scrollbar_visible["value"]:
            scrollbar.grid_remove()
            scrollbar_visible["value"] = False

    def _resize_content(event):
        canvas.itemconfigure(content_window, width=event.width)

    content.bind("<Configure>", _refresh_scrollregion)
    canvas.bind("<Configure>", _resize_content)
    # Wheel-scroll the sidebar while hovering it (same helper as acquisition);
    # the shield keeps the wheel from mutating combobox/spinbox values.
    wheel_handler = _install_mousewheel_scrolling(canvas, canvas, content)
    app.root.after_idle(lambda: _shield_wheel_value_changes(content, wheel_handler))

    def _switch_workspace(mode: str) -> None:
        app.analysis_workspace_var.set(mode)
        if mode == "mapping":
            spectrum_content.grid_remove()
            mapping_content.grid()
            app.root.sidebar_label.config(text="2D Mapping")
            if app.has_spectrum():
                app.clear_spectrum(confirm=False)
            if app.has_mapping_result():
                app.render_mapping_result(app.mapping_result)
            elif not app.has_mapping_run():
                app.clear_mapping_plot(status_text="No mapping folder loaded.")
        else:
            mapping_content.grid_remove()
            spectrum_content.grid()
            app.root.sidebar_label.config(text="Menu")
            if not app.has_spectrum():
                app.clear_spectrum(confirm=False)
        _refresh_scrollregion()
        app.refresh_data_action_state()

    ttk.Radiobutton(
        mode_frame,
        text="Spectrum",
        value="spectrum",
        variable=app.analysis_workspace_var,
        command=lambda: _switch_workspace("spectrum"),
        style="Toolbutton",
    ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
    ttk.Radiobutton(
        mode_frame,
        text="2D Mapping",
        value="mapping",
        variable=app.analysis_workspace_var,
        command=lambda: _switch_workspace("mapping"),
        style="Toolbutton",
    ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

    from prolibspector.app.launcher import MODE_CHOOSER

    app.mode_menu_btn = ttk.Button(
        mode_frame,
        text="< Mode Menu",
        command=lambda: app.request_mode_switch(MODE_CHOOSER),
    )
    app.mode_menu_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    sections = [
        (
            "Data",
            [
                ("Import Data", "Import_icon.png", functools.partial(open_import_dialog, app), False),
            ],
        ),
        (
            "Processing",
            [
                ("Adjust Spectrum", "spectrum_icon.png", functools.partial(adjust_spectrum, app, app.ax), True),
                ("Adjust Plot", "plot_icon.png", functools.partial(adjust_plot, app, app.ax), True),
                ("Adjustment Presets", "presets_icon.png", functools.partial(open_presets_dialog, app), False),
            ],
        ),
        (
            "Identification",
            [
                ("Search Element", "search_icon.png", functools.partial(periodic_table_window, app, app.ax), True),
                ("Add to Training Library", "add_to_library_icon.png", functools.partial(add_to_training_library, app), True),
                ("Apply Calibration Curve", "apply_library_icon.png", functools.partial(apply_calibration_curve_lazy, app), True),
            ],
        ),
        (
            "Export",
            [
                ("Export Plot", "export_icon.png", functools.partial(export_plot, app, app.ax), True),
                ("Export Data", "savedata_icon.png", functools.partial(export_data, app), True),
            ],
        ),
        (
            "Reset",
            [
                ("Clean Plot", "clean_icon.png", functools.partial(clean_plot, app), True),
            ],
        ),
    ]

    icon_size = ANALYSIS_BUTTON_ICON_SIZE
    section_row = 0
    for section_title, actions in sections:
        buttons_frame = ttk.LabelFrame(spectrum_content, text=section_title, padding=(6, 4))
        buttons_frame.grid(row=section_row, column=0, padx=12, pady=(2, 7), sticky="ew")
        buttons_frame.columnconfigure(0, weight=1)
        section_row += 1

        for i, (text, icon_name, command, requires_data) in enumerate(actions):
            btn = ttk.Button(
                buttons_frame,
                text=text,
                compound="left",
                command=command,
                style="LeftAligned.TButton",
                width=26,
            )
            _configure_icon(btn, icon_name, icon_size)
            btn.grid(row=i, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
            if requires_data:
                app.data_action_buttons.append(btn)

    mapping_row = 0
    app.mapping_folder_summary_var = tk.StringVar(value="No mapping folder loaded.")
    app.mapping_selected_element_var = tk.StringVar(value="No element selected.")
    app.mapping_placeholder_var = tk.StringVar(value="Single element maps intensity. Multi-element maps default to relative intensity; confidence stays available for evidence review.")
    app.mapping_map_type_var = tk.StringVar(value="single")
    app.mapping_palette_var = tk.StringVar(value="viridis")
    app.mapping_color_scale_var = tk.StringVar(value="Auto p95")
    app.mapping_show_missing_var = tk.BooleanVar(value=True)
    app.mapping_show_contours_var = tk.BooleanVar(value=False)
    # This rig's X stage direction is mirrored relative to the plot convention
    # (verified against a physical tile), so flip X by default.
    app.mapping_flip_x_var = tk.BooleanVar(value=True)
    app.mapping_flip_y_var = tk.BooleanVar(value=False)
    # Depth slider: 0 = aggregate of all pulses, 1..n = a single pulse number
    # (each successive pulse samples deeper into the crater).
    app.mapping_pulse_var = tk.DoubleVar(value=0.0)
    app.mapping_shared_scale_var = tk.BooleanVar(value=False)
    # Display-only 3x3 median for maps: on by default for single-shot runs'
    # sampling variance; raw values and exports are never altered.
    app.mapping_denoise_var = tk.BooleanVar(value=True)
    app.mapping_pulse_label_var = tk.StringVar(value="Average")
    app.mapping_multi_view_var = tk.StringVar(value="Small Multiples")
    app.mapping_rgb_r_var = tk.StringVar(value="")
    app.mapping_rgb_g_var = tk.StringVar(value="")
    app.mapping_rgb_b_var = tk.StringVar(value="")
    app.mapping_detail_element_var = tk.StringVar(value="")
    app.mapping_detail_quantity_var = tk.StringVar(value="Intensity")
    app.mapping_multi_summary_var = tk.StringVar(value="No multi-element result.")
    app.mapping_analysis_progress_var = tk.StringVar(value="Analysis cache is idle.")
    app.mapping_cache_status_var = tk.StringVar(value="Cache: none")
    app.mapping_advanced_visible_var = tk.BooleanVar(value=False)
    app.mapping_smoothing_enabled_var = tk.BooleanVar(value=False)
    app.mapping_smoothing_method_var = tk.StringVar(value="Savitzky-Golay filter")
    app.mapping_smoothing_strength_var = tk.StringVar(value="21")
    app.mapping_background_enabled_var = tk.BooleanVar(value=False)
    app.mapping_background_path_var = tk.StringVar(value="")
    app.mapping_normalization_var = tk.StringVar(value="Air-plasma reference")
    app.mapping_aggregate_var = tk.StringVar(value="median")
    app.mapping_wavelength_cal_var = tk.BooleanVar(value=True)
    app.mapping_database_var = tk.StringVar(value=DEFAULT_DATABASE_LABEL)
    app.mapping_ion_1_var = tk.BooleanVar(value=True)
    app.mapping_ion_2_var = tk.BooleanVar(value=True)
    app.mapping_ion_3_var = tk.BooleanVar(value=True)
    app.mapping_tolerance_var = tk.StringVar(value="0.3")
    app.mapping_peak_window_var = tk.StringVar(value="0.2")
    app.mapping_color_min_var = tk.StringVar(value="")
    app.mapping_color_max_var = tk.StringVar(value="")
    app.mapping_detection_preset_var = tk.StringVar(value="Normal")
    app.mapping_detected_threshold_var = tk.StringVar(value="0.75")
    app.mapping_uncertain_threshold_var = tk.StringVar(value="0.45")
    app.mapping_min_line_z_var = tk.StringVar(value="4.0")
    app.mapping_min_line_snr_var = tk.StringVar(value="5.0")
    app.mapping_require_multiline_var = tk.BooleanVar(value=True)
    app.mapping_spatial_strength_var = tk.StringVar(value="0.25")
    app.mapping_candidate_pruning_var = tk.BooleanVar(value=True)
    app.mapping_max_candidate_lines_var = tk.StringVar(value="4")
    app.mapping_parallel_workers_var = tk.StringVar(value="Auto")
    app.mapping_chunk_size_var = tk.StringVar(value="2048")
    app.mapping_cache_buttons = []
    app.mapping_multi_display_widgets = []
    app.mapping_single_display_widgets = []
    app.mapping_rgb_display_widgets = []
    app.mapping_multi_advanced_widgets = []
    app.mapping_manual_color_widgets = []
    app.mapping_analysis_busy = False

    run_frame = ttk.LabelFrame(mapping_content, text="Mapping Run", padding=(6, 4))
    run_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    run_frame.columnconfigure(0, weight=1)
    mapping_row += 1
    load_btn = ttk.Button(run_frame, text="Load Folder", compound="left", command=functools.partial(_load_mapping_folder, app), style="LeftAligned.TButton")
    _configure_icon(load_btn, "Import_icon.png", icon_size)
    load_btn.grid(row=0, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    ttk.Label(
        run_frame,
        textvariable=app.mapping_folder_summary_var,
        style="AnalysisStatus.TLabel",
        justify="left",
        wraplength=MAPPING_WRAP_LENGTH,
    ).grid(row=1, column=0, padx=6, pady=(4, 6), sticky="ew")

    type_frame = ttk.LabelFrame(mapping_content, text="Map Type", padding=(6, 4))
    type_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    type_frame.columnconfigure(0, weight=1)
    mapping_row += 1
    ttk.Radiobutton(
        type_frame,
        text="Single Element",
        variable=app.mapping_map_type_var,
        value="single",
        command=functools.partial(_refresh_mapping_display_controls, app),
    ).grid(row=0, column=0, padx=4, pady=2, sticky="w")
    ttk.Radiobutton(
        type_frame,
        text="Multi-element",
        variable=app.mapping_map_type_var,
        value="multi",
        command=functools.partial(_refresh_mapping_display_controls, app),
    ).grid(row=1, column=0, padx=4, pady=2, sticky="w")
    ttk.Label(type_frame, textvariable=app.mapping_placeholder_var, style="AnalysisStatus.TLabel", justify="left", wraplength=MAPPING_WRAP_LENGTH).grid(row=2, column=0, padx=4, pady=(3, 0), sticky="ew")

    element_frame = ttk.LabelFrame(mapping_content, text="Element", padding=(6, 4))
    element_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    element_frame.columnconfigure(0, weight=1)
    mapping_row += 1
    select_btn = ttk.Button(element_frame, text="Select Element", compound="left", command=functools.partial(_open_mapping_element_selector, app), style="LeftAligned.TButton")
    _configure_icon(select_btn, "search_icon.png", icon_size)
    select_btn.grid(row=0, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    ttk.Label(element_frame, textvariable=app.mapping_selected_element_var, style="AnalysisStatus.TLabel").grid(row=1, column=0, padx=6, pady=(2, 4), sticky="w")
    build_btn = ttk.Button(element_frame, text="Build / Update Analysis", compound="left", command=functools.partial(_apply_mapping_map, app), style="LeftAligned.TButton")
    _configure_icon(build_btn, "plot_icon.png", icon_size)
    build_btn.grid(row=2, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    app.mapping_update_buttons.append(build_btn)

    status_frame = ttk.LabelFrame(mapping_content, text="Analysis Status", padding=(6, 4))
    status_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    status_frame.columnconfigure(0, weight=1)
    mapping_row += 1
    ttk.Label(status_frame, textvariable=app.mapping_analysis_progress_var, style="AnalysisStatus.TLabel", justify="left", wraplength=MAPPING_WRAP_LENGTH).grid(
        row=0,
        column=0,
        columnspan=2,
        padx=4,
        pady=(2, 4),
        sticky="ew",
    )
    ttk.Label(status_frame, textvariable=app.mapping_cache_status_var, style="AnalysisStatus.TLabel", justify="left", wraplength=MAPPING_WRAP_LENGTH).grid(
        row=1,
        column=0,
        columnspan=2,
        padx=4,
        pady=(2, 4),
        sticky="ew",
    )
    rebuild_btn = ttk.Button(status_frame, text="Rebuild Analysis Cache", command=functools.partial(_rebuild_mapping_cache, app))
    rebuild_btn.grid(row=2, column=0, padx=4, pady=3, sticky="ew")
    clear_cache_btn = ttk.Button(status_frame, text="Clear Cache", command=functools.partial(_clear_mapping_cache, app))
    clear_cache_btn.grid(row=2, column=1, padx=4, pady=3, sticky="ew")
    app.mapping_cache_buttons.extend([rebuild_btn, clear_cache_btn])
    app.mapping_cancel_button = ttk.Button(status_frame, text="Cancel", command=functools.partial(_cancel_mapping_analysis_job, app), state="disabled")
    app.mapping_cancel_button.grid(row=3, column=0, columnspan=2, padx=4, pady=(3, 4), sticky="ew")

    display_frame = ttk.LabelFrame(mapping_content, text="Display", padding=(6, 4))
    display_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    display_frame.columnconfigure(1, weight=1)
    mapping_row += 1
    def _render_display_change(_event=None):
        _refresh_mapping_display_controls(app)
        _render_existing_mapping_result(app)

    ttk.Label(display_frame, text="Palette:").grid(row=0, column=0, padx=4, pady=4, sticky="w")
    palette_combo = ttk.Combobox(display_frame, textvariable=app.mapping_palette_var, values=MAPPING_PALETTES, state="readonly", width=16)
    palette_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    palette_combo.bind("<<ComboboxSelected>>", _render_display_change)
    color_scale_label = ttk.Label(display_frame, text="Color scale:")
    color_scale_label.grid(row=1, column=0, padx=4, pady=4, sticky="w")
    color_scale_combo = ttk.Combobox(
        display_frame,
        textvariable=app.mapping_color_scale_var,
        values=("Auto p95", "Max", "Manual"),
        state="readonly",
        width=16,
    )
    color_scale_combo.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
    color_scale_combo.bind("<<ComboboxSelected>>", _render_display_change)
    min_label = ttk.Label(display_frame, text="Min:")
    min_label.grid(row=2, column=0, padx=4, pady=4, sticky="w")
    min_entry = ttk.Entry(display_frame, textvariable=app.mapping_color_min_var, width=8)
    min_entry.grid(row=2, column=1, padx=4, pady=4, sticky="ew")
    max_label = ttk.Label(display_frame, text="Max:")
    max_label.grid(row=3, column=0, padx=4, pady=4, sticky="w")
    max_entry = ttk.Entry(display_frame, textvariable=app.mapping_color_max_var, width=8)
    max_entry.grid(row=3, column=1, padx=4, pady=4, sticky="ew")
    app.mapping_manual_color_widgets.extend([min_entry, max_entry])
    missing_check = ttk.Checkbutton(display_frame, text="Show missing spectra", variable=app.mapping_show_missing_var, command=_render_display_change, style=_themed_style("Switch.TCheckbutton", "TCheckbutton"))
    missing_check.grid(
        row=4,
        column=0,
        columnspan=2,
        padx=4,
        pady=2,
        sticky="w",
    )
    contour_check = ttk.Checkbutton(display_frame, text="Show high-intensity contours", variable=app.mapping_show_contours_var, command=_render_display_change, style=_themed_style("Switch.TCheckbutton", "TCheckbutton"))
    contour_check.grid(
        row=5,
        column=0,
        columnspan=2,
        padx=4,
        pady=2,
        sticky="w",
    )
    flip_frame = ttk.Frame(display_frame)
    flip_frame.grid(row=12, column=0, columnspan=2, padx=4, pady=2, sticky="w")
    ttk.Label(flip_frame, text="Match sample orientation:").grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(flip_frame, text="Flip X", variable=app.mapping_flip_x_var, command=_render_display_change, style=_themed_style("Switch.TCheckbutton", "TCheckbutton")).grid(row=0, column=1, sticky="w", padx=(8, 0))
    ttk.Checkbutton(flip_frame, text="Flip Y", variable=app.mapping_flip_y_var, command=_render_display_change, style=_themed_style("Switch.TCheckbutton", "TCheckbutton")).grid(row=0, column=2, sticky="w", padx=(8, 0))
    denoise_check = ttk.Checkbutton(
        display_frame,
        text="Denoise maps (3×3 median, display only)",
        variable=app.mapping_denoise_var,
        command=_render_display_change,
        style=_themed_style("Switch.TCheckbutton", "TCheckbutton"),
    )
    denoise_check.grid(row=14, column=0, columnspan=2, padx=4, pady=2, sticky="w")
    pulse_frame = ttk.Frame(display_frame)
    pulse_frame.grid(row=13, column=0, columnspan=2, padx=4, pady=(4, 2), sticky="ew")
    pulse_frame.columnconfigure(1, weight=1)
    depth_label = ttk.Label(pulse_frame, text="Depth:")
    depth_label.grid(row=0, column=0, sticky="w")
    attach_tooltip(
        depth_label,
        "Pulse number to display: each laser pulse samples deeper into the crater. "
        "Far left averages all pulses. Applies to intensity views only.",
    )

    def _on_pulse_slide(_value=None):
        app._refresh_mapping_pulse_label()

    app.mapping_pulse_scale = ttk.Scale(
        pulse_frame,
        variable=app.mapping_pulse_var,
        from_=0.0,
        to=1.0,
        orient="horizontal",
        command=_on_pulse_slide,
        state="disabled",
    )
    app.mapping_pulse_scale.grid(row=0, column=1, sticky="ew", padx=(8, 8))
    app.mapping_pulse_scale.bind("<ButtonRelease-1>", _render_display_change)
    ttk.Label(pulse_frame, textvariable=app.mapping_pulse_label_var, width=9).grid(row=0, column=2, sticky="e")
    app.mapping_single_display_widgets.extend(
        [
            color_scale_label,
            color_scale_combo,
            min_label,
            min_entry,
            max_label,
            max_entry,
            missing_check,
            contour_check,
        ]
    )
    app.mapping_multi_view_label = ttk.Label(display_frame, text="Multi view:")
    app.mapping_multi_view_label.grid(row=6, column=0, padx=4, pady=4, sticky="w")
    # Segmented control: all views visible, one click to switch.
    view_frame = ttk.Frame(display_frame)
    app.mapping_multi_view_frame = view_frame
    view_frame.grid(row=6, column=1, padx=4, pady=4, sticky="ew")
    for column in range(4):
        view_frame.columnconfigure(column, weight=1)
    segment_style = _themed_style("Toggle.TButton", "TRadiobutton")
    for column, (view_text, view_value) in enumerate(
        (
            ("Multiples", "Small Multiples"),
            ("Detail", "Element Detail"),
            ("RGB", "RGB Composite"),
            ("Confid.", "Confidence Overview"),
        )
    ):
        ttk.Radiobutton(
            view_frame,
            text=view_text,
            value=view_value,
            variable=app.mapping_multi_view_var,
            style=segment_style,
            command=_render_display_change,
        ).grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 2, 0))
    app.mapping_rgb_frame = ttk.Frame(display_frame)
    app.mapping_rgb_frame.grid(row=7, column=0, columnspan=2, padx=4, pady=4, sticky="ew")
    ttk.Label(app.mapping_rgb_frame, text="RGB channels:").grid(row=0, column=0, sticky="w")
    app.mapping_rgb_combos = []
    for _column, (_label, _var) in enumerate(
        (("R", app.mapping_rgb_r_var), ("G", app.mapping_rgb_g_var), ("B", app.mapping_rgb_b_var))
    ):
        ttk.Label(app.mapping_rgb_frame, text=_label).grid(row=0, column=1 + 2 * _column, sticky="e", padx=(8, 2))
        _combo = ttk.Combobox(app.mapping_rgb_frame, textvariable=_var, values=(), state="readonly", width=5)
        _combo.grid(row=0, column=2 + 2 * _column, sticky="w")
        _combo.bind("<<ComboboxSelected>>", _render_display_change)
        app.mapping_rgb_combos.append(_combo)
    detail_frame = ttk.Frame(display_frame)
    app.mapping_detail_frame = detail_frame
    detail_frame.grid(row=8, column=0, columnspan=2, sticky="ew", padx=4, pady=(2, 4))
    detail_frame.columnconfigure(1, weight=1)
    detail_prev_btn = ttk.Button(detail_frame, text="<", width=3, command=functools.partial(_cycle_mapping_detail_element, app, -1))
    detail_prev_btn.grid(row=0, column=0, sticky="w")
    attach_tooltip(detail_prev_btn, "Previous element")
    app.mapping_detail_element_combo = ttk.Combobox(detail_frame, textvariable=app.mapping_detail_element_var, values=(), state="readonly", width=8)
    app.mapping_detail_element_combo.grid(row=0, column=1, sticky="ew", padx=4)
    app.mapping_detail_element_combo.bind("<<ComboboxSelected>>", lambda _event: (app.mapping_multi_view_var.set("Element Detail"), _render_display_change()))
    detail_next_btn = ttk.Button(detail_frame, text=">", width=3, command=functools.partial(_cycle_mapping_detail_element, app, 1))
    detail_next_btn.grid(row=0, column=2, sticky="e")
    attach_tooltip(detail_next_btn, "Next element")
    ttk.Label(detail_frame, text="Map value:").grid(row=1, column=0, sticky="w", pady=(2, 0))
    app.mapping_detail_quantity_combo = ttk.Combobox(
        detail_frame,
        textvariable=app.mapping_detail_quantity_var,
        values=("Intensity", "Confidence"),
        state="readonly",
        width=12,
    )
    app.mapping_detail_quantity_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 0))
    app.mapping_detail_quantity_combo.bind("<<ComboboxSelected>>", _render_display_change)
    app.mapping_shared_scale_check = ttk.Checkbutton(
        display_frame,
        text="Shared intensity scale (small multiples)",
        variable=app.mapping_shared_scale_var,
        command=_render_display_change,
        style=_themed_style("Switch.TCheckbutton", "TCheckbutton"),
    )
    app.mapping_shared_scale_check.grid(row=10, column=0, columnspan=2, padx=4, pady=2, sticky="w")
    app.mapping_multi_summary_label = ttk.Label(display_frame, textvariable=app.mapping_multi_summary_var, style="AnalysisStatus.TLabel", justify="left", wraplength=MAPPING_WRAP_LENGTH)
    app.mapping_multi_summary_label.grid(
        row=9,
        column=0,
        columnspan=2,
        padx=4,
        pady=(2, 4),
        sticky="ew",
    )
    app.mapping_multi_display_widgets.extend(
        [
            app.mapping_multi_view_label,
            app.mapping_multi_view_frame,
            app.mapping_detail_frame,
            app.mapping_shared_scale_check,
            app.mapping_multi_summary_label,
        ]
    )
    app.mapping_rgb_display_widgets.extend(
        [
            app.mapping_rgb_frame,
        ]
    )

    advanced_shell = ttk.LabelFrame(mapping_content, text="Advanced", padding=(6, 4))
    advanced_shell.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    advanced_shell.columnconfigure(0, weight=1)
    mapping_row += 1
    advanced_frame = ttk.Frame(advanced_shell)
    advanced_frame.grid(row=1, column=0, sticky="ew")
    advanced_frame.columnconfigure(1, weight=1)
    advanced_frame.columnconfigure(3, weight=1)

    def _advanced_section(row_index, title):
        label = ttk.Label(advanced_frame, text=title, style="Emphasized.TLabel")
        label.grid(row=row_index, column=0, columnspan=5, sticky="w", padx=4, pady=(8, 2))
        return label

    def _toggle_advanced():
        _set_grid_visible(advanced_frame, app.mapping_advanced_visible_var.get())
        _refresh_scrollregion()

    ttk.Checkbutton(advanced_shell, text="Show advanced options", variable=app.mapping_advanced_visible_var, command=_toggle_advanced).grid(row=0, column=0, sticky="w", padx=4, pady=4)
    _advanced_section(0, "Preprocessing")
    ttk.Checkbutton(advanced_frame, text="Smooth", variable=app.mapping_smoothing_enabled_var).grid(row=1, column=0, sticky="w", padx=4, pady=2)
    ttk.Combobox(
        advanced_frame,
        textvariable=app.mapping_smoothing_method_var,
        values=("Moving average", "Gaussian filter", "Savitzky-Golay filter", "Median filter", "Wavelet transform"),
        state="readonly",
        width=20,
    ).grid(row=1, column=1, columnspan=3, sticky="ew", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Strength").grid(row=2, column=0, sticky="w", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_smoothing_strength_var, width=8).grid(row=2, column=1, sticky="ew", padx=4, pady=2)
    ttk.Checkbutton(advanced_frame, text="Subtract background", variable=app.mapping_background_enabled_var).grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=2)
    ttk.Button(advanced_frame, text="Select", command=functools.partial(_select_mapping_background_reference, app)).grid(row=3, column=2, sticky="ew", padx=4, pady=2)
    ttk.Button(advanced_frame, text="Clear", command=functools.partial(_clear_mapping_background_reference, app)).grid(row=3, column=3, sticky="ew", padx=4, pady=2)
    ttk.Label(
        advanced_frame,
        textvariable=app.mapping_background_path_var,
        style="AnalysisStatus.TLabel",
        wraplength=MAPPING_WRAP_LENGTH,
    ).grid(row=4, column=0, columnspan=5, sticky="ew", padx=4, pady=(0, 2))
    ttk.Label(
        advanced_frame,
        text="Use a laser-off reference with the same spectrometer settings; rebuild analysis after changing it.",
        style="AnalysisStatus.TLabel",
        wraplength=MAPPING_WRAP_LENGTH,
    ).grid(row=5, column=0, columnspan=5, sticky="ew", padx=4, pady=(0, 4))
    ttk.Checkbutton(advanced_frame, text="Auto wavelength calibration", variable=app.mapping_wavelength_cal_var).grid(row=6, column=0, columnspan=3, sticky="w", padx=4, pady=2)

    _advanced_section(8, "Normalization And Shots")
    ttk.Label(advanced_frame, text="Normalize").grid(row=9, column=0, sticky="w", padx=4, pady=2)
    ttk.Combobox(advanced_frame, textvariable=app.mapping_normalization_var, values=("Air-plasma reference", "Total Intensity", "None"), state="readonly", width=16).grid(row=9, column=1, columnspan=4, sticky="ew", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Shots").grid(row=10, column=0, sticky="w", padx=4, pady=2)
    ttk.Combobox(advanced_frame, textvariable=app.mapping_aggregate_var, values=("median", "mean"), state="readonly", width=16).grid(row=10, column=1, columnspan=4, sticky="ew", padx=4, pady=2)

    _advanced_section(12, "Lines And Database")
    ttk.Label(advanced_frame, text="Database").grid(row=13, column=0, sticky="w", padx=4, pady=2)
    ttk.Combobox(advanced_frame, textvariable=app.mapping_database_var, values=database_option_names(), state="readonly", width=22).grid(row=13, column=1, columnspan=4, sticky="ew", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Ionization").grid(row=14, column=0, sticky="w", padx=4, pady=2)
    ttk.Checkbutton(advanced_frame, text="1", variable=app.mapping_ion_1_var).grid(row=14, column=1, sticky="w", padx=4, pady=2)
    ttk.Checkbutton(advanced_frame, text="2", variable=app.mapping_ion_2_var).grid(row=14, column=2, sticky="w", padx=4, pady=2)
    ttk.Checkbutton(advanced_frame, text="3", variable=app.mapping_ion_3_var).grid(row=14, column=3, sticky="w", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Tolerance nm").grid(row=15, column=0, sticky="w", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_tolerance_var, width=8).grid(row=15, column=1, sticky="ew", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Window nm").grid(row=15, column=2, sticky="e", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_peak_window_var, width=8).grid(row=15, column=3, sticky="ew", padx=4, pady=2)
    ttk.Checkbutton(advanced_frame, text="Prune measured candidate lines", variable=app.mapping_candidate_pruning_var).grid(
        row=16,
        column=0,
        columnspan=5,
        sticky="w",
        padx=4,
        pady=2,
    )
    ttk.Label(advanced_frame, text="Max lines").grid(row=17, column=0, sticky="w", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_max_candidate_lines_var, width=8).grid(row=17, column=1, sticky="ew", padx=4, pady=2)

    detection_section_label = _advanced_section(18, "Detection Confidence")
    detection_preset_label = ttk.Label(advanced_frame, text="Detection preset")
    detection_preset_label.grid(row=19, column=0, sticky="w", padx=4, pady=2)
    preset_combo = ttk.Combobox(
        advanced_frame,
        textvariable=app.mapping_detection_preset_var,
        values=tuple(MULTI_ELEMENT_DETECTION_PRESETS.keys()),
        state="readonly",
        width=16,
    )
    preset_combo.grid(row=19, column=1, columnspan=4, sticky="ew", padx=4, pady=2)
    preset_combo.bind("<<ComboboxSelected>>", lambda _event: _apply_detection_preset(app))
    detected_label = ttk.Label(advanced_frame, text="Detected")
    detected_label.grid(row=20, column=0, sticky="w", padx=4, pady=2)
    detected_entry = ttk.Entry(advanced_frame, textvariable=app.mapping_detected_threshold_var, width=8)
    detected_entry.grid(row=20, column=1, sticky="ew", padx=4, pady=2)
    uncertain_label = ttk.Label(advanced_frame, text="Uncertain")
    uncertain_label.grid(row=20, column=2, sticky="e", padx=4, pady=2)
    uncertain_entry = ttk.Entry(advanced_frame, textvariable=app.mapping_uncertain_threshold_var, width=8)
    uncertain_entry.grid(row=20, column=3, sticky="ew", padx=4, pady=2)
    min_z_label = ttk.Label(advanced_frame, text="Min z")
    min_z_label.grid(row=21, column=0, sticky="w", padx=4, pady=2)
    attach_tooltip(min_z_label, "Minimum z-score a line peak must reach above the local noise floor to count as evidence.")
    min_z_entry = ttk.Entry(advanced_frame, textvariable=app.mapping_min_line_z_var, width=8)
    min_z_entry.grid(row=21, column=1, sticky="ew", padx=4, pady=2)
    min_snr_label = ttk.Label(advanced_frame, text="Min SNR")
    min_snr_label.grid(row=21, column=2, sticky="e", padx=4, pady=2)
    attach_tooltip(min_snr_label, "Minimum signal-to-noise ratio for a line to be considered usable.")
    min_snr_entry = ttk.Entry(advanced_frame, textvariable=app.mapping_min_line_snr_var, width=8)
    min_snr_entry.grid(row=21, column=3, sticky="ew", padx=4, pady=2)
    multiline_check = ttk.Checkbutton(advanced_frame, text="Require multi-line agreement", variable=app.mapping_require_multiline_var)
    multiline_check.grid(
        row=22,
        column=0,
        columnspan=5,
        sticky="w",
        padx=4,
        pady=2,
    )
    spatial_label = ttk.Label(advanced_frame, text="Spatial strength")
    spatial_label.grid(row=23, column=0, sticky="w", padx=4, pady=2)
    attach_tooltip(spatial_label, "How strongly neighboring grid points vote on a point's detection state; 0 disables spatial smoothing.")
    spatial_entry = ttk.Entry(advanced_frame, textvariable=app.mapping_spatial_strength_var, width=8)
    spatial_entry.grid(row=23, column=1, sticky="ew", padx=4, pady=2)
    confidence_note = ttk.Label(
        advanced_frame,
        text="Preset sets detection sensitivity (detected/uncertain states) only; intensity maps are unaffected.",
        style="AnalysisStatus.TLabel",
        wraplength=MAPPING_WRAP_LENGTH,
        justify="left",
    )
    confidence_note.grid(row=24, column=0, columnspan=5, sticky="ew", padx=4, pady=(2, 4))
    app.mapping_multi_advanced_widgets.extend(
        [
            detection_section_label,
            detection_preset_label,
            preset_combo,
            detected_label,
            detected_entry,
            uncertain_label,
            uncertain_entry,
            min_z_label,
            min_z_entry,
            min_snr_label,
            min_snr_entry,
            multiline_check,
            spatial_label,
            spatial_entry,
            confidence_note,
        ]
    )

    _advanced_section(25, "Performance")
    ttk.Label(advanced_frame, text="Workers").grid(row=26, column=0, sticky="w", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_parallel_workers_var, width=8).grid(row=26, column=1, sticky="ew", padx=4, pady=2)
    ttk.Label(advanced_frame, text="Chunk size").grid(row=26, column=2, sticky="e", padx=4, pady=2)
    ttk.Entry(advanced_frame, textvariable=app.mapping_chunk_size_var, width=8).grid(row=26, column=3, sticky="ew", padx=4, pady=2)
    ttk.Label(
        advanced_frame,
        text="Workers and chunk size change compute time only; results are identical.",
        style="AnalysisStatus.TLabel",
        wraplength=MAPPING_WRAP_LENGTH,
        justify="left",
    ).grid(row=27, column=0, columnspan=5, sticky="ew", padx=4, pady=(2, 4))
    restore_btn = ttk.Button(
        advanced_frame,
        text="Restore recommended settings",
        command=functools.partial(_restore_recommended_mapping_settings, app),
    )
    restore_btn.grid(row=28, column=0, columnspan=5, sticky="ew", padx=4, pady=(6, 2))
    apply_advanced_btn = ttk.Button(
        advanced_frame,
        text="Update Map",
        command=functools.partial(_apply_mapping_map, app),
        style=_themed_style("Accent.TButton", "TButton"),
    )
    apply_advanced_btn.grid(row=29, column=0, columnspan=5, sticky="ew", padx=4, pady=(0, 2))
    app.mapping_update_buttons.append(apply_advanced_btn)
    advanced_frame.grid_remove()

    export_frame = ttk.LabelFrame(mapping_content, text="Export", padding=(6, 4))
    export_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    export_frame.columnconfigure(0, weight=1)
    mapping_row += 1
    map_plot_btn = ttk.Button(export_frame, text="Export Plot", compound="left", command=functools.partial(export_plot, app, app.ax), style="LeftAligned.TButton")
    _configure_icon(map_plot_btn, "export_icon.png", icon_size)
    map_plot_btn.grid(row=0, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    app.mapping_result_action_buttons.append(map_plot_btn)
    map_data_btn = ttk.Button(export_frame, text="Export Map Data", compound="left", command=functools.partial(export_mapping_data, app), style="LeftAligned.TButton")
    _configure_icon(map_data_btn, "savedata_icon.png", icon_size)
    map_data_btn.grid(row=1, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    app.mapping_result_action_buttons.append(map_data_btn)
    spectra_csv_btn = ttk.Button(export_frame, text="Export Spectra CSVs", compound="left", command=functools.partial(export_mapping_spectra_csvs, app), style="LeftAligned.TButton")
    _configure_icon(spectra_csv_btn, "savedata_icon.png", icon_size)
    spectra_csv_btn.grid(row=2, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    app.mapping_action_buttons.append(spectra_csv_btn)

    reset_frame = ttk.LabelFrame(mapping_content, text="Reset", padding=(6, 4))
    reset_frame.grid(row=mapping_row, column=0, padx=12, pady=(2, 7), sticky="ew")
    reset_frame.columnconfigure(0, weight=1)
    clear_map_btn = ttk.Button(reset_frame, text="Clear Map", compound="left", command=functools.partial(app.clear_mapping_plot, status_text="Mapping plot cleared."), style="LeftAligned.TButton")
    _configure_icon(clear_map_btn, "clean_icon.png", icon_size)
    clear_map_btn.grid(row=0, column=0, padx=4, pady=ANALYSIS_BUTTON_PAD_Y, sticky="ew")
    app.mapping_result_action_buttons.append(clear_map_btn)

    _refresh_mapping_display_controls(app)
    app.refresh_data_action_state()
