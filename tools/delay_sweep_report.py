"""Build the full timing-sweep report for a recorded run.

Loads a plate or mapping sweep run (auto-detected), computes the per-setting
key facts, picks the recommended setting, and writes into the run folder
(or a chosen output folder):

- delay_sweep_report.html       single-file report: stats, plots, raw spectra
- delay_sweep_metrics.csv       per-setting key facts
- delay_sweep_recommendation.json
- delay_sweep_*.png             the individual figures

Usage:
    python tools/delay_sweep_report.py <run_directory> [<run_directory> ...]
        [-o OUTPUT_DIR] [--exclude-nm LO:HI ...] [--keep-laser-band]

Passing several run directories merges them into one pooled analysis
(repeat runs of the same plan are replicates: per-cell standard errors
shrink with every merged run).

By default the Nd:YAG scatter band (1054-1074 nm) is censored from all
metrics so elastic laser scatter is never scored as a line and never trips
the saturation gate. ``--exclude-nm`` adds/overrides censored bands;
``--keep-laser-band`` disables censoring entirely (only sensible with a
physical notch filter in the light path).

The output directory defaults to ``<run_directory>/delay_sweep_report``.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prolibspector.analysis.delay_sweep import (
    DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM,
    build_line_inventory,
    build_recommendation_suite,
    compute_delay_sweep_metrics,
    compute_spatial_sensitivity,
    compute_target_line_table,
    derive_target_lines,
    load_delay_sweep,
    load_delay_sweep_runs,
    recommend_trigger_delay,
    save_delay_sweep_report,
)


def _band(text: str) -> tuple[float, float]:
    low, _, high = text.partition(":")
    try:
        return (float(low), float(high))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected LO:HI in nm, got '{text}'.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_directories", nargs="+", metavar="run_directory")
    parser.add_argument("-o", "--output", dest="output_directory", default=None)
    parser.add_argument(
        "--exclude-nm",
        action="append",
        type=_band,
        default=None,
        metavar="LO:HI",
        help="Censor this wavelength band (repeatable; replaces the default laser band).",
    )
    parser.add_argument(
        "--keep-laser-band",
        action="store_true",
        help="Do not censor any wavelength band (only with a physical notch filter).",
    )
    parser.add_argument(
        "--target-lines",
        default=None,
        metavar="NM,NM,...",
        help="Comma-separated wavelengths (nm) to use as target lines for the "
        "repeatability pick (default: the strongest identified sample lines).",
    )
    args = parser.parse_args()

    run_directories = list(args.run_directories)
    merged = len(run_directories) > 1
    output_directory = args.output_directory or os.path.join(
        run_directories[0], "delay_sweep_report_merged" if merged else "delay_sweep_report"
    )
    if args.keep_laser_band:
        excluded_bands = ()
    elif args.exclude_nm:
        excluded_bands = tuple(args.exclude_nm)
    else:
        excluded_bands = DEFAULT_EXCLUDED_WAVELENGTH_BANDS_NM

    dataset = load_delay_sweep_runs(run_directories)
    metrics = compute_delay_sweep_metrics(dataset, excluded_bands_nm=excluded_bands)
    inventory = build_line_inventory(dataset, excluded_bands_nm=excluded_bands)
    if args.target_lines:
        target_lines = [(float(value), f"{float(value):.2f} nm") for value in args.target_lines.split(",") if value.strip()]
    else:
        target_lines = derive_target_lines(inventory)
    target_table = (
        compute_target_line_table(dataset, target_lines, excluded_bands_nm=excluded_bands)
        if target_lines
        else None
    )
    recommendation = build_recommendation_suite(metrics, target_table=target_table)
    spatial_sensitivity, spatial_ranking_table = compute_spatial_sensitivity(
        dataset, metrics, excluded_bands_nm=excluded_bands
    )
    recommendation["spatial_sensitivity"] = spatial_sensitivity

    if merged:
        # Per-run agreement check: does each run alone pick the same cell as
        # the pooled analysis? Disagreement flags a position/drift confound.
        per_run = []
        for run_directory in run_directories:
            try:
                run_metrics = compute_delay_sweep_metrics(
                    load_delay_sweep(run_directory), excluded_bands_nm=excluded_bands
                )
                run_pick = recommend_trigger_delay(run_metrics)
            except Exception as error:
                recommendation["reasons"].append(
                    f"Per-run check skipped for {os.path.basename(run_directory)}: {error}"
                )
                continue
            run_integration = run_pick.get("recommended_integration_us")
            per_run.append((run_pick["recommended_delay_us"], run_integration))
            setting = f"{run_pick['recommended_delay_us']:g} us"
            if run_integration is not None:
                setting += f" + {run_integration / 1000.0:g} ms"
            recommendation["reasons"].append(
                f"Per-run check: {os.path.basename(os.path.normpath(run_directory))} alone picks {setting}."
            )
        if per_run and len(set(per_run)) == 1:
            recommendation["reasons"].append(
                f"All {len(per_run)} runs individually agree with each other (no run-to-run drift signal)."
            )

    paths = save_delay_sweep_report(
        dataset, metrics, recommendation, output_directory,
        excluded_bands_nm=excluded_bands, inventory=inventory, target_table=target_table,
        spatial_sensitivity=spatial_sensitivity, spatial_ranking_table=spatial_ranking_table,
    )

    source = f"{len(run_directories)} merged runs" if merged else f"{dataset.run_type} run: {run_directories[0]}"
    print(f"Loaded {len(dataset.shots)} shots from {source}")
    if excluded_bands:
        bands_text = ", ".join(f"{low:g}-{high:g} nm" for low, high in excluded_bands)
        print(f"Censored from all metrics: {bands_text}")
    for reason in recommendation["reasons"]:
        print(f"- {reason}")
    for pick in recommendation.get("picks", {}).values():
        integration_us = pick.get("integration_us")
        setting = f"{pick['delay_us']:g} us delay"
        if integration_us is not None:
            setting += f" + {integration_us / 1000.0:g} ms integration"
        print(f"{pick['title']}: {setting} ({pick.get('stat', '')})")
    if spatial_sensitivity.get("available"):
        print(f"Spatial sensitivity: {spatial_sensitivity['conclusion']}")
    integration_us = recommendation.get("recommended_integration_us")
    setting = f"{recommendation['recommended_delay_us']:g} us delay"
    if integration_us is not None:
        setting += f" + {integration_us / 1000.0:g} ms integration"
    pick_settings = {
        (pick.get("delay_us"), pick.get("integration_us"))
        for pick in recommendation.get("picks", {}).values()
        if pick
    }
    headline = "Provisional information-rich candidate" if len(pick_settings) > 1 else "Recommended setting"
    print(f"\n{headline}: {setting}")
    print(f"Report: {paths['report_html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
