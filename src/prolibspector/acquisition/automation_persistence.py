"""Persistence helpers for automated acquisition worker outputs."""

from __future__ import annotations

from typing import Any

from prolibspector.acquisition.automation import (
    AutomatedRunState,
    AutomationRunConfig,
    save_automation_manifest,
    save_automation_run_metadata,
    save_automation_run_summary,
)


def latest_timing_summary(latest_timing_sample: Any) -> dict[str, Any]:
    if not latest_timing_sample:
        return {}
    return {"latest": latest_timing_sample}


def qc_unavailable_manifest(load_result) -> dict[str, Any]:
    return {
        "enabled": True,
        "available": False,
        "message": str(getattr(load_result, "message", "") or ""),
        "calibration_root": str(getattr(load_result, "calibration_root", "")),
        "missing_files": list(getattr(load_result, "missing_files", ()) or ()),
    }


def qc_error_manifest(exc: Exception) -> dict[str, Any]:
    return {
        "enabled": True,
        "available": True,
        "error": str(exc),
    }


def qc_success_manifest(result: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "available": True,
        "csv_path": result.csv_path,
        "pdf_path": result.pdf_path,
        "status_counts": {
            "pass": result.pass_count,
            "warn": result.warn_count,
            "fail": result.fail_count,
        },
        "failed_wells": [dict(item) for item in result.failed_well_targets],
    }


def save_worker_run_metadata(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    spectrometer_info: dict[str, Any],
    safety_checklist: dict[str, Any],
    laser_safety: dict[str, Any],
    run_mode: str,
) -> str:
    return save_automation_run_metadata(
        save_directory,
        config=config,
        spectrometer_info=spectrometer_info,
        safety_checklist=safety_checklist,
        laser_safety=laser_safety,
        run_mode=run_mode,
    )


def save_worker_automation_manifest(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState | None,
    completed_dry_run_signature: str | None,
    safety_checklist: dict[str, Any],
    command_transcript: list[str],
    qc: dict[str, Any] | None,
    laser_safety: dict[str, Any],
    event: str | None,
    run_mode: str,
) -> str:
    return save_automation_manifest(
        save_directory,
        config=config,
        state=state,
        dry_run_completed=completed_dry_run_signature == config.dry_run_signature(),
        safety_checklist=safety_checklist,
        command_transcript=command_transcript,
        qc=qc,
        laser_safety=laser_safety,
        event=event,
        run_mode=run_mode,
    )


def save_worker_automation_summary(
    save_directory: str,
    *,
    config: AutomationRunConfig,
    state: AutomatedRunState,
    latest_timing_sample: Any,
    run_mode: str,
) -> str:
    return save_automation_run_summary(
        save_directory,
        config=config,
        state=state,
        timing_summary=latest_timing_summary(latest_timing_sample),
        run_mode=run_mode,
    )
