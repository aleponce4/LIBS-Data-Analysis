"""Shared spectrometer correction resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EffectiveCorrectionSettings:
    requested_dark_counts: bool
    requested_nonlinearity: bool
    correct_dark_counts: bool
    correct_nonlinearity: bool
    dark_counts_reason: str = ""
    nonlinearity_reason: str = ""

    def to_kwargs(self) -> dict[str, bool]:
        return {
            "correct_dark_counts": self.correct_dark_counts,
            "correct_nonlinearity": self.correct_nonlinearity,
        }

    def mask_reasons(self) -> dict[str, str]:
        reasons = {}
        if self.dark_counts_reason:
            reasons["dark_counts"] = self.dark_counts_reason
        if self.nonlinearity_reason:
            reasons["nonlinearity"] = self.nonlinearity_reason
        return reasons

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "requested_dark_counts": self.requested_dark_counts,
            "requested_nonlinearity": self.requested_nonlinearity,
            "effective_dark_counts": self.correct_dark_counts,
            "effective_nonlinearity": self.correct_nonlinearity,
        }
        reasons = self.mask_reasons()
        if reasons:
            payload["mask_reasons"] = reasons
        return payload


def resolve_effective_corrections(
    capabilities,
    *,
    requested_dark_counts: bool,
    requested_nonlinearity: bool,
) -> EffectiveCorrectionSettings:
    """Resolve requested correction checkboxes against spectrometer support."""
    supports_dark = bool(getattr(capabilities, "supports_dark_correction", False))
    supports_nl = bool(getattr(capabilities, "supports_nonlinearity_correction", False))
    requested_dark = bool(requested_dark_counts)
    requested_nl = bool(requested_nonlinearity)
    return EffectiveCorrectionSettings(
        requested_dark_counts=requested_dark,
        requested_nonlinearity=requested_nl,
        correct_dark_counts=bool(requested_dark and supports_dark),
        correct_nonlinearity=bool(requested_nl and supports_nl),
        dark_counts_reason=(
            "unsupported_by_spectrometer" if requested_dark and not supports_dark else ""
        ),
        nonlinearity_reason=(
            "unsupported_by_spectrometer" if requested_nl and not supports_nl else ""
        ),
    )
