"""Shared laser-band definitions and display scaling helpers for spectra."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


# The Nd:YAG fundamental can dominate a LIBS spectrum without helping an
# operator see the weaker emission lines. The band is deliberately wider
# than a single pixel because saturated elastic scatter can bloom around
# 1064 nm. Samples in this band remain plotted and saved; only automatic
# Y-axis limit calculations ignore them. Timing analysis also uses the same
# band so its metrics and plots agree about which peak is laser scatter.
NDYAG_FUNDAMENTAL_NM = 1064.0
DEFAULT_NDYAG_EXCLUDED_WAVELENGTH_BANDS_NM: tuple[tuple[float, float], ...] = (
    (1054.0, 1074.0),
)


def excluded_wavelength_mask(
    wavelengths: Sequence[float] | np.ndarray,
    excluded_bands_nm: Sequence[tuple[float, float]] | None,
) -> np.ndarray:
    """Return a mask of wavelength pixels inside any excluded band."""
    wavelengths_array = np.asarray(wavelengths, dtype=float)
    mask = np.zeros(wavelengths_array.shape, dtype=bool)
    for band in excluded_bands_nm or ():
        low, high = float(band[0]), float(band[1])
        if high < low:
            low, high = high, low
        mask |= (wavelengths_array >= low) & (wavelengths_array <= high)
    return mask


def spectrum_autoscale_peak(
    wavelengths: Sequence[float] | np.ndarray,
    intensities: Sequence[float] | np.ndarray,
    *,
    excluded_bands_nm: Sequence[tuple[float, float]] | None = DEFAULT_NDYAG_EXCLUDED_WAVELENGTH_BANDS_NM,
) -> float:
    """Return the finite nonnegative peak that should drive display scaling.

    ``intensities`` may be one spectrum or a stack whose final dimension is
    the wavelength dimension. Excluded samples are still present in the
    caller's plotted data. If no usable sample exists outside the excluded
    bands, the full finite spectrum is used as a safe fallback.
    """
    wavelengths_array = np.asarray(wavelengths, dtype=float).reshape(-1)
    intensity_array = np.asarray(intensities, dtype=float)
    if intensity_array.size == 0:
        return 0.0

    candidates = intensity_array
    if (
        wavelengths_array.size
        and intensity_array.ndim >= 1
        and intensity_array.shape[-1] == wavelengths_array.size
    ):
        keep = ~excluded_wavelength_mask(wavelengths_array, excluded_bands_nm)
        if keep.any():
            outside = intensity_array[..., keep]
            if np.isfinite(outside).any():
                candidates = outside

    finite = candidates[np.isfinite(candidates)]
    if not finite.size:
        finite = intensity_array[np.isfinite(intensity_array)]
    if not finite.size:
        return 0.0
    return max(0.0, float(np.max(finite)))
