"""Benchmark triggered acquisition throughput.

This runner measures the acquisition worker end-to-end and records phase
timings for:

- trigger wait / readout
- wavelength fetch
- spectrum CSV save
- plate-state JSON save
- GUI queue latency, when run through AcquisitionApp

Typical uses:

    python acquisition_benchmark.py --simulate --mode test --shots 25
    python acquisition_benchmark.py --real --backend ocean_optics --mode armed --shots 10
    python acquisition_benchmark.py --simulate --mode live --gui
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import multiprocessing
import queue
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from prolibspector.acquisition.corrections import resolve_effective_corrections
from prolibspector.acquisition.worker import AcquisitionMessage, AcquisitionWorker
from prolibspector.acquisition.plate_autosave import PlateAutosaveConfig


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]

    rank = (len(values) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return values[low]
    low_weight = high - rank
    high_weight = rank - low
    return values[low] * low_weight + values[high] * high_weight


def duration_ms(sample: dict[str, Any], start_key: str, end_key: str) -> float | None:
    start = sample.get(start_key)
    end = sample.get(end_key)
    if start is None or end is None:
        return None
    return (float(end) - float(start)) * 1000.0


def build_spectrometer(backend: str, simulate: bool, profile: str, device_index: int):
    from prolibspector.hardware.spectrometer import SpectrometerModule, ThorlabsCCSModule

    if backend == "ocean_optics":
        spec = SpectrometerModule()
        if simulate:
            status = spec.connect_simulated(profile)
        else:
            status = spec.connect(device_index=device_index)
        return spec, status

    if backend == "thorlabs":
        spec = ThorlabsCCSModule()
        if simulate:
            status = spec.connect_simulated(profile)
        else:
            status = spec.connect(device_index=device_index)
        return spec, status

    if backend == "yixist":
        if simulate:
            spec = SpectrometerModule()
            status = spec.connect_simulated(profile)
        else:
            from prolibspector.hardware.yixist_spectrometer_broker import YixistSpectrometerBrokerClient

            spec = YixistSpectrometerBrokerClient()
            status = spec.connect(device_index=device_index)
        return spec, status

    raise ValueError(f"Unsupported backend '{backend}'.")


def prepare_worker(
    spectrometer,
    *,
    save_directory: Path,
    sample_name: str,
    auto_save: bool,
    armed_poll_ms: float,
    live_poll_ms: float,
    averages: int,
    plate_config: PlateAutosaveConfig | None,
) -> AcquisitionWorker:
    worker = AcquisitionWorker(spectrometer)
    worker.auto_save_enabled = auto_save
    worker.save_directory = str(save_directory)
    worker.sample_name = sample_name
    worker.armed_poll_interval = max(0.0, armed_poll_ms / 1000.0)
    worker.live_poll_interval = max(0.0, live_poll_ms / 1000.0)
    worker.averages = max(1, averages)
    worker.enable_timing_metrics(True)

    if plate_config is not None:
        worker.set_plate_autosave_config(plate_config)

    worker.start()
    return worker


def drain_worker_queue(worker: AcquisitionWorker):
    """Drain remaining messages and raise on any worker error."""
    while True:
        try:
            msg_type, data = worker.message_queue.get_nowait()
        except queue.Empty:
            break

        if msg_type == AcquisitionMessage.ERROR:
            raise RuntimeError(str(data))


def wait_for_worker_message(
    worker: AcquisitionWorker,
    message_type: str,
    timeout_s: float,
):
    deadline = time.perf_counter() + timeout_s
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for worker message {message_type!r}.")
        try:
            msg_type, data = worker.message_queue.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if msg_type == AcquisitionMessage.ERROR:
            raise RuntimeError(str(data))
        if msg_type == message_type:
            return data


def spectrum_stats(label: str, wavelengths, intensities, *, attempt_index: int = 1) -> dict[str, Any]:
    wl = np.asarray(wavelengths, dtype=float)
    y = np.asarray(intensities, dtype=float)
    return {
        "source": label,
        "attempt_index": int(attempt_index),
        "pixel_count": int(y.size),
        "wavelength_min": float(np.nanmin(wl)) if wl.size else None,
        "wavelength_max": float(np.nanmax(wl)) if wl.size else None,
        "intensity_min": float(np.nanmin(y)) if y.size else None,
        "intensity_p05": float(np.nanpercentile(y, 5)) if y.size else None,
        "intensity_median": float(np.nanmedian(y)) if y.size else None,
        "intensity_p95": float(np.nanpercentile(y, 95)) if y.size else None,
        "intensity_max": float(np.nanmax(y)) if y.size else None,
        "intensity_mean": float(np.nanmean(y)) if y.size else None,
        "intensity_std": float(np.nanstd(y)) if y.size else None,
    }


def print_spectrum_stats(row: dict[str, Any]) -> None:
    if row.get("pixel_count") is None:
        print(f"{row['source']}: {row}")
        return
    print(
        f"{row['source']} #{row['attempt_index']}: "
        f"pixels={row['pixel_count']} "
        f"wl={row['wavelength_min']:.1f}-{row['wavelength_max']:.1f} nm "
        f"median={row['intensity_median']:.3f} "
        f"std={row['intensity_std']:.3f} "
        f"min={row['intensity_min']:.3f} "
        f"max={row['intensity_max']:.3f}"
    )


def run_no_save_diagnostics(spectrometer, *, shots: int, timeout_s: float) -> list[dict[str, Any]]:
    """Run Ocean Optics troubleshooting reads without saving spectra or firing a laser."""
    caps = getattr(spectrometer, "capabilities", None)
    sample_count = max(1, int(shots))
    rows: list[dict[str, Any]] = []

    print("=== No-save Spectrometer Diagnostics ===")
    print(f"Spectrometer class: {type(spectrometer).__name__}")
    print(f"Model: {getattr(caps, 'model', 'unknown')}")
    print(f"Serial: {getattr(caps, 'serial_number', 'unknown')}")
    print(f"SeaBreeze backend: {getattr(spectrometer, 'seabreeze_backend', None) or 'n/a'}")
    fallback = getattr(spectrometer, "seabreeze_backend_fail_reason", None)
    if fallback:
        print(f"Backend fallback reason: {fallback}")
    print(f"Supports dark correction: {bool(getattr(caps, 'supports_dark_correction', False))}")
    print(f"Supports nonlinearity correction: {bool(getattr(caps, 'supports_nonlinearity_correction', False))}")

    normal_mode = getattr(caps, "normal_trigger_mode", None)
    external_mode = getattr(caps, "external_trigger_mode", None)
    if normal_mode is not None:
        spectrometer.set_trigger_mode(normal_mode)
    wavelengths = spectrometer.get_wavelengths()

    raw_kwargs = {"correct_dark_counts": False, "correct_nonlinearity": False}
    effective = resolve_effective_corrections(
        caps,
        requested_dark_counts=True,
        requested_nonlinearity=True,
    )
    print("Requested corrections: dark=on, nonlinearity=on")
    print(
        "Effective corrections: "
        f"dark={'on' if effective.correct_dark_counts else 'off'}, "
        f"nonlinearity={'on' if effective.correct_nonlinearity else 'off'}"
    )
    if effective.mask_reasons():
        print(f"Correction mask reasons: {effective.mask_reasons()}")

    for attempt_index in range(1, sample_count + 1):
        raw = spectrometer.get_intensities(**raw_kwargs)
        row = spectrum_stats("direct_raw", wavelengths, raw, attempt_index=attempt_index)
        rows.append(row)
        print_spectrum_stats(row)

    for attempt_index in range(1, sample_count + 1):
        corrected = spectrometer.get_intensities(**effective.to_kwargs())
        row = spectrum_stats("direct_effective", wavelengths, corrected, attempt_index=attempt_index)
        rows.append(row)
        print_spectrum_stats(row)

    if external_mode is not None and normal_mode is not None:
        spectrometer.set_trigger_mode(external_mode)
        after_external = getattr(spectrometer, "current_trigger_mode", None)
        spectrometer.set_trigger_mode(normal_mode)
        after_normal = getattr(spectrometer, "current_trigger_mode", None)
        trigger_row = {
            "source": "trigger_mode_check",
            "attempt_index": 1,
            "normal_mode": normal_mode,
            "external_mode": external_mode,
            "after_external": after_external,
            "after_normal": after_normal,
        }
        rows.append(trigger_row)
        print(
            "Trigger mode check: "
            f"external={external_mode} observed={after_external}, "
            f"normal={normal_mode} observed={after_normal}"
        )
    else:
        print("Trigger mode check skipped: external trigger mode is unavailable.")

    worker = prepare_worker(
        spectrometer,
        save_directory=Path(tempfile.mkdtemp(prefix="libs_ocean_diag_")),
        sample_name="OceanDiagnostic",
        auto_save=False,
        armed_poll_ms=50.0,
        live_poll_ms=20.0,
        averages=1,
        plate_config=None,
    )
    try:
        worker.start_live()
        live_wavelengths, live_intensities = wait_for_worker_message(
            worker,
            AcquisitionMessage.SPECTRUM,
            timeout_s,
        )
        row = spectrum_stats("worker_live", live_wavelengths, live_intensities)
        rows.append(row)
        print_spectrum_stats(row)
        worker.go_idle()
        time.sleep(0.05)
        drain_worker_queue(worker)

        worker.test_trigger()
        test_wavelengths, test_intensities = wait_for_worker_message(
            worker,
            AcquisitionMessage.SPECTRUM,
            timeout_s,
        )
        row = spectrum_stats("worker_test_capture", test_wavelengths, test_intensities)
        rows.append(row)
        print_spectrum_stats(row)
        worker.go_idle()
    finally:
        worker.stop()
        worker.join(timeout=3)

    return rows


def wait_for_headless_timing_sample(worker: AcquisitionWorker, timeout_s: float) -> dict[str, Any]:
    deadline = time.perf_counter() + timeout_s

    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for a timing sample.")

        try:
            msg_type, data = worker.message_queue.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue

        if msg_type == AcquisitionMessage.TIMING:
            sample = dict(data)
            sample["queue_received_at"] = time.perf_counter()
            if "worker_enqueued_at" in sample:
                sample["queue_latency"] = sample["queue_received_at"] - float(sample["worker_enqueued_at"])
            return sample

        if msg_type == AcquisitionMessage.ERROR:
            raise RuntimeError(str(data))


def wait_for_gui_timing_sample(app, expected_count: int, timeout_s: float) -> dict[str, Any]:
    deadline = time.perf_counter() + timeout_s
    while len(app.timing_samples) < expected_count:
        if time.perf_counter() > deadline:
            raise TimeoutError("Timed out waiting for a GUI timing sample.")
        app._poll_queue(reschedule=False)
        try:
            app.root.update_idletasks()
        except Exception:
            pass
        time.sleep(0.005)
    return dict(app.timing_samples[expected_count - 1])


def run_headless(
    worker: AcquisitionWorker,
    *,
    mode: str,
    shots: int,
    timeout_s: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if mode == "test":
        for attempt_index in range(1, shots + 1):
            worker.test_trigger()
            try:
                sample = wait_for_headless_timing_sample(worker, timeout_s)
            except TimeoutError:
                rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                continue
            sample["attempt_index"] = attempt_index
            sample["timed_out"] = False
            rows.append(sample)
        return rows

    if mode == "live":
        worker.start_live()
        try:
            for attempt_index in range(1, shots + 1):
                try:
                    sample = wait_for_headless_timing_sample(worker, timeout_s)
                except TimeoutError:
                    rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                    continue
                sample["attempt_index"] = attempt_index
                sample["timed_out"] = False
                rows.append(sample)
        finally:
            worker.go_idle()
        return rows

    if mode == "armed":
        worker.arm_trigger()
        try:
            for attempt_index in range(1, shots + 1):
                try:
                    sample = wait_for_headless_timing_sample(worker, timeout_s)
                except TimeoutError:
                    rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                    continue
                sample["attempt_index"] = attempt_index
                sample["timed_out"] = False
                rows.append(sample)
        finally:
            worker.go_idle()
        return rows

    raise ValueError(f"Unsupported mode '{mode}'.")


def run_gui(
    app,
    *,
    mode: str,
    shots: int,
    timeout_s: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if mode == "test":
        for attempt_index in range(1, shots + 1):
            app.worker.test_trigger()
            try:
                wait_for_gui_timing_sample(app, len(app.timing_samples) + 1, timeout_s)
            except TimeoutError:
                rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                continue
            sample = dict(app.timing_samples[-1])
            sample["attempt_index"] = attempt_index
            sample["timed_out"] = False
            rows.append(sample)
        return rows

    if mode == "live":
        app.worker.start_live()
        try:
            for attempt_index in range(1, shots + 1):
                try:
                    wait_for_gui_timing_sample(app, len(app.timing_samples) + 1, timeout_s)
                except TimeoutError:
                    rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                    continue
                sample = dict(app.timing_samples[-1])
                sample["attempt_index"] = attempt_index
                sample["timed_out"] = False
                rows.append(sample)
        finally:
            app.worker.go_idle()
        return rows

    if mode == "armed":
        app.worker.arm_trigger()
        try:
            for attempt_index in range(1, shots + 1):
                try:
                    wait_for_gui_timing_sample(app, len(app.timing_samples) + 1, timeout_s)
                except TimeoutError:
                    rows.append({"attempt_index": attempt_index, "mode": mode, "timed_out": True})
                    continue
                sample = dict(app.timing_samples[-1])
                sample["attempt_index"] = attempt_index
                sample["timed_out"] = False
                rows.append(sample)
        finally:
            app.worker.go_idle()
        return rows

    raise ValueError(f"Unsupported mode '{mode}'.")


def enrich_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for sample in samples:
        row = dict(sample)
        if row.get("timed_out"):
            enriched.append(row)
            continue
        row["trigger_wait_ms"] = duration_ms(row, "trigger_wait_start", "trigger_wait_end")
        row["capture_ms"] = duration_ms(row, "trigger_wait_start", "capture_end")
        if row["capture_ms"] is None:
            row["capture_ms"] = duration_ms(row, "cycle_start", "capture_end")
        row["wavelength_fetch_ms"] = duration_ms(row, "wavelengths_fetch_start", "wavelengths_fetch_end")
        row["save_ms"] = duration_ms(row, "save_start", "save_end")
        row["plate_state_write_ms"] = duration_ms(row, "plate_state_write_start", "plate_state_write_end")
        row["total_ms"] = duration_ms(row, "trigger_wait_start", "rearm_start")
        if row["total_ms"] is None:
            row["total_ms"] = duration_ms(row, "cycle_start", "message_sent")
        if row["total_ms"] is None:
            row["total_ms"] = duration_ms(row, "cycle_start", "gui_received_at")
        if row["total_ms"] is None:
            row["total_ms"] = duration_ms(row, "cycle_start", "cycle_end")
        if row.get("gui_queue_latency") is None and "worker_enqueued_at" in row and "gui_received_at" in row:
            row["gui_queue_latency"] = float(row["gui_received_at"]) - float(row["worker_enqueued_at"])
        enriched.append(row)
    return enriched


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in samples if not row.get("timed_out")]
    timed_out = [row for row in samples if row.get("timed_out")]

    total_ms = [float(row["total_ms"]) for row in successful if row.get("total_ms") is not None]
    save_ms = [float(row["save_ms"]) for row in successful if row.get("save_ms") is not None]
    plate_write_ms = [float(row["plate_state_write_ms"]) for row in successful if row.get("plate_state_write_ms") is not None]
    trigger_ms = [float(row["trigger_wait_ms"]) for row in successful if row.get("trigger_wait_ms") is not None]
    gui_ms = [float(row["gui_queue_latency"]) * 1000.0 for row in successful if row.get("gui_queue_latency") is not None]

    summary = {
        "attempt_count": len(samples),
        "success_count": len(successful),
        "timeout_count": len(timed_out),
        "total_ms": {
            "p50": percentile(total_ms, 50),
            "p95": percentile(total_ms, 95),
            "p99": percentile(total_ms, 99),
            "mean": statistics.fmean(total_ms) if total_ms else None,
            "max": max(total_ms) if total_ms else None,
        },
        "save_ms": {
            "p50": percentile(save_ms, 50),
            "p95": percentile(save_ms, 95),
            "p99": percentile(save_ms, 99),
            "mean": statistics.fmean(save_ms) if save_ms else None,
            "max": max(save_ms) if save_ms else None,
        },
        "plate_state_write_ms": {
            "p50": percentile(plate_write_ms, 50),
            "p95": percentile(plate_write_ms, 95),
            "p99": percentile(plate_write_ms, 99),
            "mean": statistics.fmean(plate_write_ms) if plate_write_ms else None,
            "max": max(plate_write_ms) if plate_write_ms else None,
        },
        "trigger_wait_ms": {
            "p50": percentile(trigger_ms, 50),
            "p95": percentile(trigger_ms, 95),
            "p99": percentile(trigger_ms, 99),
            "mean": statistics.fmean(trigger_ms) if trigger_ms else None,
            "max": max(trigger_ms) if trigger_ms else None,
        },
        "gui_queue_latency_ms": {
            "p50": percentile(gui_ms, 50),
            "p95": percentile(gui_ms, 95),
            "p99": percentile(gui_ms, 99),
            "mean": statistics.fmean(gui_ms) if gui_ms else None,
            "max": max(gui_ms) if gui_ms else None,
        },
    }
    return summary


def print_summary(summary: dict[str, Any], *, mode: str, backend: str, simulate: bool, save_directory: Path, auto_save: bool, plate_config: PlateAutosaveConfig | None, armed_poll_ms: float, live_poll_ms: float):
    print("=== Acquisition Benchmark ===")
    print(f"Mode: {mode}")
    print(f"Backend: {backend} {'(simulated)' if simulate else '(real)'}")
    print(f"Attempts: {summary['attempt_count']}")
    print(f"Successful samples: {summary['success_count']}")
    print(f"Timeouts: {summary['timeout_count']}")
    print(f"Auto-save: {'on' if auto_save else 'off'}")
    print(f"Save directory: {save_directory}")
    print(f"Armed poll interval: {armed_poll_ms:.1f} ms")
    print(f"Live poll interval: {live_poll_ms:.1f} ms")
    if plate_config is not None:
        print(
            f"Plate mode: on ({plate_config.plate_type}-well, "
            f"{plate_config.shots_per_well} shot(s)/well, {plate_config.order_mode})"
        )
    else:
        print("Plate mode: off")

    def line(label: str, payload: dict[str, Any], unit: str = "ms"):
        def fmt(value: Any) -> str:
            return "n/a" if value is None else f"{float(value):.3f}{unit}"

        print(
            f"{label:<22} p50={fmt(payload['p50'])} "
            f"p95={fmt(payload['p95'])} "
            f"p99={fmt(payload['p99'])} "
            f"mean={fmt(payload['mean'])} "
            f"max={fmt(payload['max'])}"
        )

    if summary["success_count"] > 0:
        line("Total", summary["total_ms"])
    if summary["save_ms"]["p50"] is not None:
        line("Save", summary["save_ms"])
    if summary["plate_state_write_ms"]["p50"] is not None:
        line("Plate JSON", summary["plate_state_write_ms"])
    if summary["trigger_wait_ms"]["p50"] is not None:
        line("Trigger wait", summary["trigger_wait_ms"])
    if summary["gui_queue_latency_ms"]["p50"] is not None:
        line("GUI queue", summary["gui_queue_latency_ms"], unit="ms")

    print("\nTarget-rate check")
    for hz in (10, 20, 50, 100):
        budget = 1000.0 / hz
        p95 = summary["total_ms"]["p95"]
        p99 = summary["total_ms"]["p99"]
        if p95 is None or p99 is None:
            verdict = "insufficient data"
        elif p99 <= budget:
            verdict = "pass"
        elif p95 <= budget:
            verdict = "borderline"
        else:
            verdict = "risk"
        margin = None if p99 is None else budget - p99
        margin_text = "n/a" if margin is None else f"{margin:.3f} ms"
        p95_text = "n/a" if p95 is None else f"{p95:.3f} ms"
        p99_text = "n/a" if p99 is None else f"{p99:.3f} ms"
        print(f"{hz:>3} Hz budget {budget:7.3f} ms | p95={p95_text} | p99={p99_text} | margin={margin_text} | {verdict}")


def write_output(path: Path, samples: list[dict[str, Any]], summary: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        fieldnames = sorted({key for sample in samples for key in sample.keys()})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for sample in samples:
                writer.writerow(sample)
        return

    payload = {
        "summary": summary,
        "samples": samples,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark LIBS acquisition throughput.")
    parser.add_argument("--mode", choices=("test", "live", "armed", "diagnose"), default="test")
    parser.add_argument("--backend", choices=("ocean_optics", "thorlabs", "yixist"), default="ocean_optics")
    parser.add_argument("--simulate", dest="simulate", action="store_true", help="Use a simulated spectrometer.")
    parser.add_argument("--real", dest="simulate", action="store_false", help="Use connected hardware.")
    parser.set_defaults(simulate=True)
    parser.add_argument("--profile", default="USB4000", help="Simulation profile name.")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--shots", type=int, default=25)
    parser.add_argument("--save-directory", default=None, help="Directory used for saved CSV files.")
    parser.add_argument("--sample-name", default="Benchmark")
    parser.add_argument("--auto-save", dest="auto_save", action="store_true", help="Enable CSV auto-save.")
    parser.add_argument("--no-auto-save", dest="auto_save", action="store_false", help="Disable CSV auto-save.")
    parser.set_defaults(auto_save=True)
    parser.add_argument("--plate-mode", action="store_true", help="Enable plate autosave benchmarking.")
    parser.add_argument("--plate-name", default="BenchmarkPlate")
    parser.add_argument("--plate-type", type=int, default=96)
    parser.add_argument("--shots-per-well", type=int, default=1)
    parser.add_argument("--order-mode", choices=("row", "column"), default="row")
    parser.add_argument("--gui", action="store_true", help="Run through AcquisitionApp to measure GUI queue lag.")
    parser.add_argument("--armed-poll-ms", type=float, default=50.0)
    parser.add_argument("--live-poll-ms", type=float, default=20.0)
    parser.add_argument("--averages", type=int, default=1)
    parser.add_argument("--timeout-sec", type=float, default=None)
    parser.add_argument("--output", type=str, default=None, help="Write a CSV or JSON report to this path.")
    return parser.parse_args()


def main():
    args = parse_args()

    save_directory = Path(args.save_directory) if args.save_directory else Path(tempfile.mkdtemp(prefix="libs_benchmark_"))
    plate_config = None
    if args.plate_mode:
        plate_config = PlateAutosaveConfig(
            plate_type=args.plate_type,
            plate_name=args.plate_name,
            shots_per_well=max(1, args.shots_per_well),
            order_mode=args.order_mode,
        )
        if not args.auto_save:
            args.auto_save = True

    timeout_s = args.timeout_sec
    if timeout_s is None:
        base = 2.0
        if args.mode == "armed":
            base = max(base, args.shots * 5.0)
        elif args.mode == "live":
            base = max(base, args.shots * (args.live_poll_ms / 1000.0) * 4.0)
        elif args.mode == "diagnose":
            base = max(base, args.shots * 2.0)
        timeout_s = base

    spectrometer = None
    worker = None
    app = None
    root = None

    try:
        spectrometer, status = build_spectrometer(
            args.backend,
            args.simulate,
            args.profile,
            args.device_index,
        )
        print(status)

        if args.mode == "diagnose":
            if args.gui:
                raise ValueError("--mode diagnose is headless only; omit --gui.")
            samples = run_no_save_diagnostics(
                spectrometer,
                shots=args.shots,
                timeout_s=timeout_s,
            )
            summary = {
                "mode": "diagnose",
                "backend": args.backend,
                "simulate": args.simulate,
                "sample_count": len(samples),
                "seabreeze_backend": getattr(spectrometer, "seabreeze_backend", None),
                "seabreeze_backend_fail_reason": getattr(
                    spectrometer,
                    "seabreeze_backend_fail_reason",
                    None,
                ),
            }
            if args.output:
                write_output(Path(args.output), samples, summary)
                print(f"\nWrote report: {args.output}")
            return

        if args.gui:
            import tkinter as tk
            import sv_ttk
            from prolibspector.acquisition.app import AcquisitionApp

            root = tk.Tk()
            try:
                sv_ttk.set_theme("light")
            except Exception:
                pass
            root.withdraw()

            app = AcquisitionApp(root)
            app.spectrometer = spectrometer
            app.save_dir_var.set(str(save_directory))
            app.sample_name_var.set(args.sample_name)
            app.auto_save_var.set(args.auto_save)
            app.plate_mode_var.set(bool(plate_config))
            app._finish_connection(status)
            app._cancel_queue_poll()
            worker = app.worker
            if worker is None:
                raise RuntimeError("Acquisition worker did not start.")
            worker.enable_timing_metrics(True)
            worker.armed_poll_interval = max(0.0, args.armed_poll_ms / 1000.0)
            worker.live_poll_interval = max(0.0, args.live_poll_ms / 1000.0)
            worker.averages = max(1, args.averages)
            worker.save_directory = str(save_directory)
            worker.sample_name = args.sample_name
            if plate_config is not None:
                worker.set_plate_autosave_config(plate_config)

            samples = run_gui(app, mode=args.mode, shots=args.shots, timeout_s=timeout_s)
        else:
            worker = prepare_worker(
                spectrometer,
                save_directory=save_directory,
                sample_name=args.sample_name,
                auto_save=args.auto_save,
                armed_poll_ms=args.armed_poll_ms,
                live_poll_ms=args.live_poll_ms,
                averages=args.averages,
                plate_config=plate_config,
            )
            samples = run_headless(worker, mode=args.mode, shots=args.shots, timeout_s=timeout_s)

        samples = enrich_samples(samples)
        summary = summarize(samples)
        print_summary(
            summary,
            mode=args.mode,
            backend=args.backend,
            simulate=args.simulate,
            save_directory=save_directory,
            auto_save=args.auto_save,
            plate_config=plate_config,
            armed_poll_ms=args.armed_poll_ms,
            live_poll_ms=args.live_poll_ms,
        )

        if args.output:
            write_output(Path(args.output), samples, summary)
            print(f"\nWrote report: {args.output}")

    finally:
        if app is not None:
            try:
                app._cleanup_and_close()
                # Teardown is asynchronous; pump events until it finishes.
                deadline = time.time() + 10
                while not getattr(app, "_ui_closing", True) and time.time() < deadline:
                    app.root.update()
                    time.sleep(0.01)
            except Exception:
                pass
            app = None
            worker = None
            spectrometer = None
            root = None
        if worker is not None:
            try:
                worker.stop()
                worker.join(timeout=3)
            except Exception:
                pass
        if spectrometer is not None:
            try:
                spectrometer.disconnect()
            except Exception:
                pass
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
