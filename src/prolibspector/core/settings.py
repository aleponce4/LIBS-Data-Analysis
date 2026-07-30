"""Settings helpers for local preset persistence."""

import json
import logging
import os
from datetime import datetime

from prolibspector.core.paths import settings_dir

logger = logging.getLogger(__name__)

DEFAULT_UI_SETTINGS = {
    "theme": "Light",
    "default_export_dpi": 300,
    "confirm_clean_reset": True,
    # Last-used acquisition choices restored at startup (empty = use the
    # built-in default). Persisted once per session at shutdown.
    "last_save_dir": "",
    "last_sample_name": "",
    "last_integration_ms": "",
    "last_laser_baud": "",
    "last_laser_port": "",
    "last_jog_step": "",
    "last_jog_feed": "",
    "last_laser_profile": "",
    "last_run_type": "",
}


def ui_prefs_enabled() -> bool:
    """Session-preference persistence kill switch (tests set it to 0)."""
    return os.environ.get("PROLIBSPECTOR_UI_PREFS", "1") != "0"


def get_ui_pref(key: str, fallback: str = "") -> str:
    """Return a persisted last-used value, or *fallback* when unset/disabled."""
    if not ui_prefs_enabled():
        return fallback
    value = get_ui_settings().get(key, "")
    text = str(value).strip() if value is not None else ""
    return text or fallback


def default_save_directory(*, use_pref: bool = True) -> str:
    """Parent folder new run directories are created in.

    ``PROLIBSPECTOR_DEFAULT_SAVE_DIR`` overrides everything — the test
    suite points it at a scratch folder so no test can ever write into the
    operator's real data directory. Otherwise the operator's last-used
    folder (when ``use_pref``), then ``~/LIBS_Data``.
    """
    override = str(os.environ.get("PROLIBSPECTOR_DEFAULT_SAVE_DIR", "") or "").strip()
    if override:
        return override
    fallback = os.path.join(os.path.expanduser("~"), "LIBS_Data")
    if use_pref:
        return get_ui_pref("last_save_dir", fallback)
    return fallback


def save_ui_prefs(prefs: dict) -> None:
    """Persist last-used values (no-op when preference saving is disabled)."""
    if not ui_prefs_enabled() or not prefs:
        return
    save_ui_settings(prefs)


def get_settings_path():
    """Get the appropriate settings path based on environment"""
    path = settings_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / "settings.json"


def save_settings(settings_dict):
    """Save all adjustment settings to JSON file"""
    tmp_path = None
    try:
        settings_path = get_settings_path()
        tmp_path = settings_path.with_name(f"{settings_path.name}.tmp")
        with open(tmp_path, 'w', encoding="utf-8") as f:
            json.dump(settings_dict, f, indent=2)
        os.replace(tmp_path, settings_path)
        return True, "Settings saved successfully"
    except Exception as e:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False, f"Error saving settings: {str(e)}"


def load_settings():
    """Load settings from JSON file, return None if file doesn't exist"""
    settings_path = get_settings_path()
    if not settings_path.exists():
        return None

    try:
        with open(settings_path, 'r', encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        backup_path = settings_path.with_name(
            f"{settings_path.stem}.corrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}{settings_path.suffix}"
        )
        try:
            settings_path.replace(backup_path)
            logger.warning("Moved corrupt settings file to %s: %s", backup_path, exc)
        except OSError:
            logger.warning("Settings file is corrupt and could not be moved: %s", exc)
        return None
    except OSError as exc:
        logger.warning("Could not load settings from %s: %s", settings_path, exc)
        return None


def delete_settings():
    """Delete the settings file"""
    try:
        settings_path = get_settings_path()
        if settings_path.exists():
            settings_path.unlink()
        return True, "Settings deleted successfully"
    except Exception as e:
        return False, f"Error deleting settings: {str(e)}"


def get_default_settings():
    """Return the default settings"""
    return {
        "adjust_spectrum": {
            "smoothing_method": "Moving average",
            "smoothing_strength": 1,
            "laser_removal_enabled": False,
            "laser_wavelength": 532.63,
            "laser_removal_width": 2.0,
            "baseline_removal_enabled": False,
            "baseline_smoothness": "Medium (1e4)",
            "baseline_asymmetry": "Balanced (0.001)"
        },
        "adjust_plot": {
            "x_axis_start": 100.0,
            "x_axis_end": 1000.0,
            "y_axis_start": 0.0,
            "y_axis_end": 1.0,
            "normalize_method": "None",
            "line_color": "#000000",
            "background_color": "#FFFFFF",
            "line_width": 1.0
        },
        "ui": DEFAULT_UI_SETTINGS.copy()
    }


def get_ui_settings():
    """Return UI preferences with defaults filled in."""
    settings = load_settings() or {}
    ui_settings = DEFAULT_UI_SETTINGS.copy()
    ui_settings.update(settings.get("ui", {}))

    try:
        ui_settings["default_export_dpi"] = max(1, int(ui_settings["default_export_dpi"]))
    except (TypeError, ValueError):
        ui_settings["default_export_dpi"] = DEFAULT_UI_SETTINGS["default_export_dpi"]
    ui_settings["confirm_clean_reset"] = bool(ui_settings.get("confirm_clean_reset", True))
    theme = str(ui_settings.get("theme", "Light")).title()
    ui_settings["theme"] = theme if theme in {"Light", "Dark"} else "Light"
    return ui_settings


def save_ui_settings(ui_settings):
    """Merge UI preferences into the existing settings JSON."""
    settings = load_settings() or get_default_settings()
    merged_ui = get_ui_settings()
    merged_ui.update(ui_settings)
    settings["ui"] = merged_ui
    return save_settings(settings)


def capture_spectrum_settings(smooth_method_var, smooth_strength_slider, 
                             laser_removal_var, laser_wavelength_var, 
                             laser_width_var, baseline_removal_var,
                             smoothness_preset_var, asymmetry_preset_var):
    """Capture current spectrum adjustment settings from GUI variables"""
    settings = {
        "smoothing_method": smooth_method_var.get(),
        "smoothing_strength": int(float(smooth_strength_slider.get())),
        "laser_removal_enabled": laser_removal_var.get(),
        "laser_wavelength": laser_wavelength_var.get(),
        "laser_removal_width": laser_width_var.get(),
        "baseline_removal_enabled": baseline_removal_var.get(),
        "baseline_smoothness": smoothness_preset_var.get(),
        "baseline_asymmetry": asymmetry_preset_var.get()
    }
    return settings


def apply_spectrum_settings(settings, smooth_method_var, smooth_strength_slider,
                           laser_removal_var, laser_wavelength_var,
                           laser_width_var, baseline_removal_var,
                           smoothness_preset_var, asymmetry_preset_var):
    """Apply saved spectrum settings to GUI variables"""
    if not settings or "adjust_spectrum" not in settings:
        return False
    
    try:
        s = settings["adjust_spectrum"]
        smooth_method_var.set(s.get("smoothing_method", "Moving average"))
        smooth_strength_slider.set(s.get("smoothing_strength", 1))
        laser_removal_var.set(s.get("laser_removal_enabled", False))
        laser_wavelength_var.set(s.get("laser_wavelength", 532.63))
        laser_width_var.set(s.get("laser_removal_width", 2.0))
        baseline_removal_var.set(s.get("baseline_removal_enabled", False))
        smoothness_preset_var.set(s.get("baseline_smoothness", "Medium (1e4)"))
        asymmetry_preset_var.set(s.get("baseline_asymmetry", "Balanced (0.001)"))
        return True
    except Exception as e:
        print(f"Error applying settings: {str(e)}")
        return False


def capture_plot_settings(x_start_var, x_end_var, y_start_var, y_end_var,
                         line_color_var, bg_color_var, line_width_var, normalize_var):
    """Capture current plot adjustment settings from GUI variables"""
    settings = {
        "x_axis_start": x_start_var.get(),
        "x_axis_end": x_end_var.get(),
        "y_axis_start": y_start_var.get(),
        "y_axis_end": y_end_var.get(),
        "line_color": line_color_var.get(),
        "background_color": bg_color_var.get(),
        "line_width": line_width_var.get(),
        "normalize_method": normalize_var.get()
    }
    return settings


def apply_plot_settings(settings, x_start_var, x_end_var, y_start_var, y_end_var,
                       line_color_var, bg_color_var, line_width_var, normalize_var):
    """Apply saved plot settings to GUI variables"""
    if not settings or "adjust_plot" not in settings:
        return False
    
    try:
        p = settings["adjust_plot"]
        x_start_var.set(p.get("x_axis_start", 100.0))
        x_end_var.set(p.get("x_axis_end", 1000.0))
        y_start_var.set(p.get("y_axis_start", 0.0))
        y_end_var.set(p.get("y_axis_end", 1.0))
        line_color_var.set(p.get("line_color", "#000000"))
        bg_color_var.set(p.get("background_color", "#FFFFFF"))
        line_width_var.set(p.get("line_width", 1.0))
        # Backward compat: old settings stored normalize_enabled as bool
        method = p.get("normalize_method", None)
        if method is None:
            method = "Min-Max" if p.get("normalize_enabled", False) else "None"
        normalize_var.set(method)
        return True
    except Exception as e:
        print(f"Error applying plot settings: {str(e)}")
        return False
