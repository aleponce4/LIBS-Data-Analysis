"""Dialog for adding labelled peaks to the local calibration library.

PUBLIC-EDITION MODULE
=====================
This is a public-edition implementation written against the single call site in
``analysis/workflows.py`` (``add_to_training_library``).

What this does: takes the peaks currently matched on the plot
(``app.peak_data``, rows of ``[wavelength, element, ionization_level,
relative_intensity]``), asks for the reference concentration and units, and
appends them to ``calibration_data_library.csv`` through the existing
``analysis.calibration_library`` helpers. That library is what
``analysis/calibration_curve.py`` reads to fit calibration curves, so the round
trip is fully functional here.

What the private edition adds: the same entries are also pushed to the
commercial training corpus used to fit the private quantification models
(shared library sync, provenance signing, and per-instrument response
normalisation). None of that infrastructure is part of the public edition, so
this dialog writes to the local CSV only and says so on screen.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from prolibspector.analysis.calibration_library import (
    CALIBRATION_LIBRARY_COLUMNS,
    append_calibration_entries,
    calibration_library_path,
)
from prolibspector.core.ui_scale import scaled_font, scaled_wrap, ui_scale_for_widget
from prolibspector.core.windowing import apply_window_policy, create_dialog

logger = logging.getLogger(__name__)

DEFAULT_UNITS = "ppm"
UNIT_CHOICES = ("ppm", "ppb", "wt%", "mg/kg", "mol/L")

DIALOG_PREFERRED_SIZE = (620, 460)
DIALOG_MIN_SIZE = (520, 400)


def _peak_rows(app) -> list[list]:
    """Return the currently matched peaks as library-shaped rows."""
    rows = []
    for entry in getattr(app, "peak_data", None) or []:
        if len(entry) < 4:
            continue
        wavelength, element, ionization_level, relative_intensity = entry[:4]
        rows.append([wavelength, element, ionization_level, relative_intensity])
    return rows


def open_training_library_dialog(app) -> None:
    """Show the Add-to-Library dialog for the app's current matched peaks."""
    rows = _peak_rows(app)
    if not rows:
        messagebox.showinfo(
            "No Labelled Peaks",
            "There are no matched peaks to add.\n\n"
            "Label peaks against the element database first, then add them to the library.",
            parent=app.root,
        )
        return

    dialog = create_dialog(app.root, "Add to Calibration Library", modal=True, resizable=True)
    scale = ui_scale_for_widget(dialog)

    outer = ttk.Frame(dialog, padding=14)
    outer.pack(fill=tk.BOTH, expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(2, weight=1)

    ttk.Label(
        outer,
        text=f"Add {len(rows)} matched peak(s) to the calibration library",
        font=scaled_font(13, "bold", scale=scale),
    ).grid(row=0, column=0, sticky="w", pady=(0, 6))

    ttk.Label(
        outer,
        text=(
            "Entries are appended to the local calibration library CSV that Apply "
            "Calibration Curve reads.\n"
            f"Library file: {calibration_library_path()}\n\n"
            "Public edition: entries stay on this machine. The private edition also "
            "syncs them to the commercial training corpus."
        ),
        style="Status.TLabel",
        wraplength=scaled_wrap(560, scale),
        justify=tk.LEFT,
    ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

    form = ttk.LabelFrame(outer, text="Reference Values", padding=10)
    form.grid(row=2, column=0, sticky="nsew")
    form.columnconfigure(1, weight=1)

    concentration_var = tk.StringVar(value="")
    units_var = tk.StringVar(value=DEFAULT_UNITS)

    ttk.Label(form, text="Concentration:").grid(row=0, column=0, sticky="w", pady=4)
    concentration_entry = ttk.Entry(form, textvariable=concentration_var, width=18)
    concentration_entry.grid(row=0, column=1, sticky="w", pady=4)
    concentration_entry.focus_set()

    ttk.Label(form, text="Units:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Combobox(
        form,
        textvariable=units_var,
        values=list(UNIT_CHOICES),
        width=16,
    ).grid(row=1, column=1, sticky="w", pady=4)

    preview = ttk.Treeview(
        form,
        columns=("wavelength", "element", "ionization", "intensity"),
        show="headings",
        height=8,
    )
    for column, heading, width in (
        ("wavelength", "Wavelength (nm)", 130),
        ("element", "Element", 90),
        ("ionization", "Ionization", 90),
        ("intensity", "Intensity", 130),
    ):
        preview.heading(column, text=heading)
        preview.column(column, width=width, anchor="w")
    preview.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
    form.rowconfigure(2, weight=1)
    for row in rows:
        preview.insert("", "end", values=tuple(str(value) for value in row))

    def _submit() -> None:
        raw_concentration = concentration_var.get().strip()
        try:
            concentration = float(raw_concentration)
        except ValueError:
            messagebox.showwarning(
                "Invalid Concentration",
                "Enter the reference concentration as a number.",
                parent=dialog,
            )
            return
        units = units_var.get().strip() or DEFAULT_UNITS

        entries = [[*row, concentration, units] for row in rows]
        try:
            library_path = append_calibration_entries(entries)
        except OSError as exc:
            logger.exception("Could not append to the calibration library.")
            messagebox.showerror("Write Failed", str(exc), parent=dialog)
            return

        dialog.destroy()
        messagebox.showinfo(
            "Added to Library",
            f"Added {len(entries)} entry/entries to:\n{library_path}\n\n"
            f"Columns: {', '.join(CALIBRATION_LIBRARY_COLUMNS)}",
            parent=app.root,
        )
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status(
                f"Added {len(entries)} calibration entry/entries at {concentration} {units}."
            )

    button_bar = ttk.Frame(outer)
    button_bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    ttk.Button(button_bar, text="Cancel", width=12, command=dialog.destroy).pack(side=tk.RIGHT, padx=(6, 0))
    ttk.Button(button_bar, text="Add to Library", width=18, command=_submit).pack(side=tk.RIGHT)
    dialog.bind("<Return>", lambda _event: _submit())

    apply_window_policy(
        dialog,
        parent=app.root,
        preferred=DIALOG_PREFERRED_SIZE,
        min_size=DIALOG_MIN_SIZE,
        modal=True,
        resizable=True,
    )
    dialog.wait_window()


__all__ = ["open_training_library_dialog"]
