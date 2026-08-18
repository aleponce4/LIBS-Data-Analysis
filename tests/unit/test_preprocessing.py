"""Tests for spectral preprocessing functions."""

import numpy as np
from prolibspector.analysis.adjust_spectrum import (
    apply_baseline_removal,
    apply_smoothing,
)


def test_apply_smoothing_savitzky_golay():
    rng = np.random.default_rng(42)
    x = np.linspace(0, 10, 100)
    clean = np.sin(x) + 2.0
    noisy = clean + rng.normal(0, 0.1, 100)

    smoothed = apply_smoothing(noisy, method="Savitzky-Golay filter", strength=11)
    assert len(smoothed) == 100
    assert np.all(np.isfinite(smoothed))


def test_apply_baseline_removal_als():
    x = np.linspace(200, 800, 500)
    baseline_true = 50.0 + 0.1 * (x - 200)
    peak = 500.0 * np.exp(-0.5 * ((x - 500) / 10.0) ** 2)
    spectrum = baseline_true + peak

    corrected = apply_baseline_removal(spectrum, lam=1e5, p=0.01)
    assert len(corrected) == 500
    assert np.all(np.isfinite(corrected))
    # Baseline should be reduced away from peak
    assert np.abs(np.mean(corrected[:50])) < 15.0


def test_apply_smoothing_gaussian():
    y = np.array([10.0, 20.0, 50.0, 100.0, 20.0, 10.0])
    smoothed = apply_smoothing(y, method="Gaussian filter", strength=2.0)
    assert len(smoothed) == len(y)
    assert np.all(np.isfinite(smoothed))
