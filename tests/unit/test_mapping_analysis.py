"""Unit and contract tests for 2D spatial mapping analysis and seeded element detection."""

import os
import json
import tempfile
import numpy as np
import pytest

from prolibspector.analysis.mapping_analysis import (
    MappingPreprocessConfig,
    MultiElementMapConfig,
    build_multi_element_map,
    load_mapping_run,
)


def test_mapping_output_preserves_coordinates_and_count():
    """Verify that multi-element mapping preserves grid dimensions and point count."""
    fixture_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "ProLIBSpector",
        "prolibspector",
        "analysis",
        "fixtures",
        "example_mapping_run",
    )
    fixture_dir = os.path.abspath(fixture_dir)

    if not os.path.exists(fixture_dir):
        pytest.skip("Fixture directory not found")

    run = load_mapping_run(fixture_dir)
    assert run.grid_rows == 5
    assert run.grid_columns == 5
    assert run.points_total == 25

    pre = MappingPreprocessConfig()
    cfg = MultiElementMapConfig.from_preset(("Na", "Fe"), preset="Normal")
    res = build_multi_element_map(run, pre, cfg)

    assert "Na" in res.element_intensity_grids
    na_grid = np.asarray(res.element_intensity_grids["Na"], dtype=float)
    assert na_grid.shape == (5, 5)
    assert np.isfinite(na_grid).any()


def test_known_synthetic_element_seeded_region_signal():
    """Verify that a synthetic element seeded in a specific quadrant produces higher map signal."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        grid_rows, grid_cols = 4, 4
        wavelengths = np.arange(200.0, 950.0, 0.05, dtype=np.float64)
        width = 0.2

        filenames = []
        for r in range(grid_rows):
            for c in range(grid_cols):
                idx = r * grid_cols + c
                fname = f"shot_{idx:03d}.csv"
                filenames.append(fname)
                fpath = os.path.join(tmp_dir, fname)

                rng = np.random.default_rng(idx + 100)
                base_noise = rng.normal(50.0, 2.0, len(wavelengths))
                o_peak = 5000.0 * np.exp(-0.5 * ((wavelengths - 777.4) / width) ** 2)

                # Seed Sodium (Na) lines ONLY in top-right quadrant (r >= 2 and c >= 2)
                if r >= 2 and c >= 2:
                    peaks = (
                        20000.0 * np.exp(-0.5 * ((wavelengths - 894.296) / width) ** 2)
                        + 15000.0 * np.exp(-0.5 * ((wavelengths - 780.978) / width) ** 2)
                        + 12000.0 * np.exp(-0.5 * ((wavelengths - 752.033) / width) ** 2)
                    )
                    y = base_noise + o_peak + peaks
                else:
                    y = base_noise + o_peak

                with open(fpath, "w", encoding="utf-8") as f:
                    f.write("Wavelength,Intensity\n")
                    for wl, val in zip(wavelengths, y):
                        f.write(f"{wl:.3f},{val:.3f}\n")

        # Write index CSV with required schema
        index_path = os.path.join(tmp_dir, "_mapping_grid_index.csv")
        headers = [
            "row", "column", "x_mm", "y_mm", "shot_number", "binary_row_index",
            "captured_at", "column_index", "filename", "filepath",
            "integration_time_us", "point_key", "row_index", "shot_index", "target_key"
        ]
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(",".join(headers) + "\n")
            for r in range(grid_rows):
                for c in range(grid_cols):
                    idx = r * grid_cols + c
                    fname = filenames[idx]
                    f.write(
                        f"{r},{c},{c * 1.0},{r * 1.0},1,{idx},"
                        f"2026-07-30T12:00:00.000000,{c},{fname},{fname},"
                        f"80,R{r}C{c},{r},0,T{idx}\n"
                    )

        # Write manifest JSON
        manifest_path = os.path.join(tmp_dir, "_mapping_grid_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"sample_label": "TestSeededNaMatched", "grid_rows": 4, "grid_columns": 4}, f)

        # Load run and build multi-element map
        run = load_mapping_run(tmp_dir)
        assert run.grid_rows == 4
        assert run.grid_columns == 4

        pre = MappingPreprocessConfig(normalization="Total Intensity")
        cfg = MultiElementMapConfig.from_preset(("Na", "Fe"), preset="Exploratory")
        res = build_multi_element_map(run, pre, cfg)

        na_grid = np.asarray(res.element_intensity_grids["Na"], dtype=float)
        assert na_grid.shape == (4, 4)
        assert np.isfinite(na_grid).all()

        # Quadrant signal comparison: seeded quadrant vs unseeded quadrant
        seeded_region_mean = np.nanmean(na_grid[2:, 2:])
        unseeded_region_mean = np.nanmean(na_grid[:2, :2])

        assert seeded_region_mean > 0.8
        assert unseeded_region_mean < 0.01
        assert seeded_region_mean > unseeded_region_mean * 50.0
