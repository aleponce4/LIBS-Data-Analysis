"""Generate example timing-sweep plots and a recommendation.

Runs the simulated generic gated detector backend through the mapping-style joint
sweep (trigger delay by grid column, integration time by row band, several
shots per cell, metal-blank assumption), then produces the same report the
analysis module writes for a real sweep run:

- delay_sweep_spectra.png       mean spectrum per delay (recommended integration)
- delay_sweep_metrics.png       SNR/SBR vs delay, one line per integration
- delay_sweep_heatmap.png       SNR heatmap over the delay x integration grid
- delay_sweep_metrics.csv       per-cell key facts
- delay_sweep_recommendation.json

Usage:
    python tools/delay_sweep_example.py [output_directory]

Default output directory: docs/img/delay_sweep_example
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from prolibspector.acquisition.automation_mapping import (
    MAPPING_JOINT_SWEEP_COLUMN_DELAYS_US,
    MAPPING_JOINT_SWEEP_ROW_INTEGRATION_TIMES_MS,
)
from prolibspector.analysis.delay_sweep import (
    DelaySweepDataset,
    DelaySweepShot,
    compute_delay_sweep_metrics,
    recommend_trigger_delay,
    save_delay_sweep_report,
)
from prolibspector.hardware import SpectrometerModule

SHOTS_PER_CELL = 10


def acquire_simulated_sweep() -> DelaySweepDataset:
    spec = SpectrometerModule()
    try:
        spec.connect_simulated("GatedCCD")
        wavelengths = spec.get_wavelengths()
        shots: list[DelaySweepShot] = []
        for band, integration_ms in enumerate(MAPPING_JOINT_SWEEP_ROW_INTEGRATION_TIMES_MS, start=1):
            integration_us = int(round(integration_ms * 1000.0))
            spec.set_integration_time(integration_us)
            applied_integration_us = float(spec.integration_time_us)
            for column, delay_us in enumerate(MAPPING_JOINT_SWEEP_COLUMN_DELAYS_US, start=1):
                spec.set_trigger_delay(delay_us)
                for shot_number in range(1, SHOTS_PER_CELL + 1):
                    row = (band - 1) * SHOTS_PER_CELL + shot_number
                    shots.append(
                        DelaySweepShot(
                            delay_us=float(delay_us),
                            intensities=np.asarray(spec.get_intensities(), dtype=float),
                            integration_us=applied_integration_us,
                            source=f"R{row:03d}:C{column:03d}:S01",
                        )
                    )
        return DelaySweepDataset(
            wavelengths=np.asarray(wavelengths, dtype=float),
            shots=shots,
            run_type="mapping",
            run_directory="<simulated generic gated detector joint sweep>",
            column_delays_us=tuple(MAPPING_JOINT_SWEEP_COLUMN_DELAYS_US),
            integration_times_ms=tuple(MAPPING_JOINT_SWEEP_ROW_INTEGRATION_TIMES_MS),
            max_intensity=float(spec.capabilities.max_intensity),
        )
    finally:
        spec.disconnect()


def main() -> int:
    output_directory = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "img", "delay_sweep_example")
    )
    dataset = acquire_simulated_sweep()
    metrics = compute_delay_sweep_metrics(dataset)
    recommendation = recommend_trigger_delay(metrics)
    paths = save_delay_sweep_report(dataset, metrics, recommendation, output_directory)

    display = metrics[
        ["delay_us", "integration_time_us", "shots", "continuum_level", "noise_level", "snr", "sbr", "saturation_fraction"]
    ]
    print(display.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print()
    for reason in recommendation["reasons"]:
        print(f"- {reason}")
    integration_us = recommendation.get("recommended_integration_us")
    setting = f"{recommendation['recommended_delay_us']:g} us delay"
    if integration_us is not None:
        setting += f" + {integration_us / 1000.0:g} ms integration"
    print(f"\nRecommended setting: {setting}")
    for key, path in paths.items():
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
