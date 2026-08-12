"""Tests for the plot-level normalization transforms."""

import numpy as np
from prolibspector.analysis.adjust_plot import (
    normalize_min_max,
    normalize_snv,
    normalize_total_intensity,
)


def test_normalize_min_max_maps_to_unit_range():
    y = np.array([5.0, 10.0, 15.0, 20.0])
    out = normalize_min_max(y)
    assert out.min() == 0.0
    assert out.max() == 1.0


def test_normalize_min_max_flat_trace_is_zeros():
    out = normalize_min_max(np.full(8, 3.0))
    assert np.all(out == 0.0)


def test_normalize_total_intensity_sums_to_one():
    y = np.array([1.0, 2.0, 3.0, 4.0])
    out = normalize_total_intensity(y)
    assert np.isclose(out.sum(), 1.0)


def test_normalize_snv_is_zero_mean_unit_std():
    rng = np.random.default_rng(7)
    y = rng.normal(500.0, 80.0, 256)

    out = normalize_snv(y)

    assert out.shape == y.shape
    assert np.isclose(out.mean(), 0.0)
    assert np.isclose(out.std(), 1.0)


def test_normalize_snv_removes_multiplicative_scaling():
    """The point of SNV: a gain change must not alter the transformed spectrum."""
    x = np.linspace(200, 800, 400)
    spectrum = 20.0 + 300.0 * np.exp(-0.5 * ((x - 500) / 8.0) ** 2)

    baseline = normalize_snv(spectrum)
    amplified = normalize_snv(spectrum * 4.7)

    assert np.allclose(baseline, amplified)


def test_normalize_snv_flat_trace_does_not_divide_by_zero():
    out = normalize_snv(np.full(16, 42.0))
    assert np.all(out == 0.0)
    assert np.all(np.isfinite(out))
