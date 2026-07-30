"""Plate model library for automated acquisition geometry."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from prolibspector.acquisition.plate_autosave import PLATE_FORMATS
from prolibspector.core.settings import get_default_settings, load_settings, save_settings


SETTINGS_SECTION = "automated_acquisition"
PLATE_MODELS_KEY = "plate_models"
DEFAULT_96_PLATE_MODEL_ID = "corning_costar_96"
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PlateModel:
    id: str
    display_name: str
    well_count: int
    rows: int
    columns: int
    width_mm: float
    height_mm: float
    a1_center_x_mm: float
    a1_center_y_mm: float
    pitch_x_mm: float
    pitch_y_mm: float
    verified: bool = False
    notes: str = ""

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "PlateModel":
        return cls(
            id=str(values["id"]).strip(),
            display_name=str(values.get("display_name") or values["id"]).strip(),
            well_count=int(values["well_count"]),
            rows=int(values["rows"]),
            columns=int(values["columns"]),
            width_mm=float(values["width_mm"]),
            height_mm=float(values["height_mm"]),
            a1_center_x_mm=float(values["a1_center_x_mm"]),
            a1_center_y_mm=float(values["a1_center_y_mm"]),
            pitch_x_mm=float(values["pitch_x_mm"]),
            pitch_y_mm=float(values["pitch_y_mm"]),
            verified=bool(values.get("verified", False)),
            notes=str(values.get("notes", "") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "well_count": self.well_count,
            "rows": self.rows,
            "columns": self.columns,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "a1_center_x_mm": self.a1_center_x_mm,
            "a1_center_y_mm": self.a1_center_y_mm,
            "pitch_x_mm": self.pitch_x_mm,
            "pitch_y_mm": self.pitch_y_mm,
            "verified": self.verified,
            "notes": self.notes,
        }

    @property
    def status_label(self) -> str:
        return "verified" if self.verified else "unverified"

    @property
    def combo_label(self) -> str:
        return f"{self.display_name} ({self.status_label})"


_GENERIC_PITCH_MM = {
    6: (39.12, 39.12),
    12: (26.00, 26.00),
    24: (19.30, 19.30),
    48: (13.00, 13.00),
    96: (9.00, 9.00),
    384: (4.50, 4.50),
}


def _generic_model(well_count: int, rows: int, columns: int) -> PlateModel:
    width_mm = 127.76
    height_mm = 85.48
    pitch_x_mm, pitch_y_mm = _GENERIC_PITCH_MM[well_count]
    a1_x = (width_mm - ((columns - 1) * pitch_x_mm)) / 2.0
    a1_y = (height_mm - ((rows - 1) * pitch_y_mm)) / 2.0
    return PlateModel(
        id=f"generic_{well_count}",
        display_name=f"Generic {well_count}-well plate",
        well_count=well_count,
        rows=rows,
        columns=columns,
        width_mm=width_mm,
        height_mm=height_mm,
        a1_center_x_mm=a1_x,
        a1_center_y_mm=a1_y,
        pitch_x_mm=pitch_x_mm,
        pitch_y_mm=pitch_y_mm,
        verified=False,
        notes="Starter geometry only. Replace with measured/vendor-specific coordinates before production use.",
    )


def _corning_costar_96_model() -> PlateModel:
    return PlateModel(
        id=DEFAULT_96_PLATE_MODEL_ID,
        display_name="Corning/Costar 96-well plate",
        well_count=96,
        rows=8,
        columns=12,
        width_mm=127.8,
        height_mm=85.5,
        a1_center_x_mm=14.3,
        a1_center_y_mm=11.2,
        pitch_x_mm=9.0,
        pitch_y_mm=9.0,
        verified=True,
        notes=(
            "Corning/Costar common 96-well plate dimensions from vendor dimension sheet. "
            "Verify physical fixture origin and usable laser bed before production runs."
        ),
    )


def bundled_plate_models() -> list[PlateModel]:
    generic_models = [
        _generic_model(well_count, rows, columns)
        for well_count, (rows, columns) in PLATE_FORMATS.items()
        if well_count in _GENERIC_PITCH_MM
    ]
    return [_corning_costar_96_model(), *generic_models]


def user_plate_models_from_settings() -> list[PlateModel]:
    settings = load_settings() or {}
    raw_models = (
        settings.get(SETTINGS_SECTION, {})
        .get(PLATE_MODELS_KEY, [])
    )
    models = []
    if not isinstance(raw_models, list):
        return models
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        try:
            models.append(PlateModel.from_mapping(item))
        except (KeyError, TypeError, ValueError):
            continue
    return models


def load_plate_model_library() -> list[PlateModel]:
    """Return bundled models plus any user-defined settings models.

    User models with the same id override bundled starter models.
    """
    by_id = {model.id: model for model in bundled_plate_models()}
    for model in user_plate_models_from_settings():
        by_id[model.id] = model
    return sorted(by_id.values(), key=lambda model: (model.well_count, model.display_name.lower()))


def plate_model_by_id(model_id: str) -> PlateModel:
    for model in load_plate_model_library():
        if model.id == model_id:
            return model
    raise KeyError(f"Unknown plate model: {model_id}")


def validate_plate_model(model: PlateModel) -> list[str]:
    errors: list[str] = []
    if not model.id or not _MODEL_ID_RE.match(model.id):
        errors.append("Model id must contain only letters, numbers, dots, underscores, or hyphens.")
    if not model.display_name.strip():
        errors.append("Display name is required.")
    if model.well_count not in PLATE_FORMATS:
        errors.append(f"Unsupported well count: {model.well_count}.")
    else:
        expected_rows, expected_columns = PLATE_FORMATS[model.well_count]
        if model.rows != expected_rows or model.columns != expected_columns:
            errors.append(
                f"{model.well_count}-well models must use {expected_rows} rows x {expected_columns} columns."
            )
    for name, value in [
        ("width_mm", model.width_mm),
        ("height_mm", model.height_mm),
        ("a1_center_x_mm", model.a1_center_x_mm),
        ("a1_center_y_mm", model.a1_center_y_mm),
        ("pitch_x_mm", model.pitch_x_mm),
        ("pitch_y_mm", model.pitch_y_mm),
    ]:
        if value <= 0:
            errors.append(f"{name} must be greater than zero.")
    last_x = model.a1_center_x_mm + ((model.columns - 1) * model.pitch_x_mm)
    last_y = model.a1_center_y_mm + ((model.rows - 1) * model.pitch_y_mm)
    if last_x > model.width_mm:
        errors.append("Last column center is outside the plate width.")
    if last_y > model.height_mm:
        errors.append("Last row center is outside the plate height.")
    return errors


def _settings_with_automation_section() -> dict[str, Any]:
    settings = load_settings() or get_default_settings()
    section = settings.setdefault(SETTINGS_SECTION, {})
    if not isinstance(section, dict):
        section = {}
        settings[SETTINGS_SECTION] = section
    section.setdefault(PLATE_MODELS_KEY, [])
    return settings


def save_user_plate_models(models: list[PlateModel]) -> tuple[bool, str]:
    unique: dict[str, PlateModel] = {}
    for model in models:
        errors = validate_plate_model(model)
        if errors:
            return False, "; ".join(errors)
        unique[model.id] = model
    settings = _settings_with_automation_section()
    settings[SETTINGS_SECTION][PLATE_MODELS_KEY] = [
        model.to_mapping()
        for model in sorted(unique.values(), key=lambda item: (item.well_count, item.display_name.lower()))
    ]
    return save_settings(settings)


def upsert_user_plate_model(model: PlateModel) -> tuple[bool, str]:
    errors = validate_plate_model(model)
    if errors:
        return False, "; ".join(errors)
    models = {existing.id: existing for existing in user_plate_models_from_settings()}
    models[model.id] = model
    return save_user_plate_models(list(models.values()))


def delete_user_plate_model(model_id: str) -> tuple[bool, str]:
    models = [model for model in user_plate_models_from_settings() if model.id != model_id]
    return save_user_plate_models(models)


def duplicate_plate_model(
    source: PlateModel,
    *,
    new_id: str,
    display_name: str,
    verified: bool = False,
    notes: str | None = None,
) -> PlateModel:
    model = PlateModel(
        id=new_id.strip(),
        display_name=display_name.strip(),
        well_count=source.well_count,
        rows=source.rows,
        columns=source.columns,
        width_mm=source.width_mm,
        height_mm=source.height_mm,
        a1_center_x_mm=source.a1_center_x_mm,
        a1_center_y_mm=source.a1_center_y_mm,
        pitch_x_mm=source.pitch_x_mm,
        pitch_y_mm=source.pitch_y_mm,
        verified=bool(verified),
        notes=source.notes if notes is None else notes,
    )
    errors = validate_plate_model(model)
    if errors:
        raise ValueError("; ".join(errors))
    return model
