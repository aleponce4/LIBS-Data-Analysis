"""Collapsible right-side status and settings inspector."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from prolibspector.core.settings import get_ui_settings, save_ui_settings
from prolibspector.core.ui_scale import apply_text_scale, scaled_font, scaled_wrap, ui_scale_for_widget


COLLAPSED_WIDTH = 48
EXPANDED_WIDTH = 300


class RightInspector(ttk.Frame):
    """A narrow right rail that expands into Status and Settings tabs."""

    def __init__(self, parent, app, *, mode_name: str, status_var: tk.StringVar, switch_label: str):
        super().__init__(parent, width=COLLAPSED_WIDTH)
        self.app = app
        self.mode_name = mode_name
        self.status_var = status_var
        self.switch_label = switch_label
        self.expanded = False
        self._refresh_after_id = None
        self.summary_var = tk.StringVar(value="")
        self.spectrometer_var = tk.StringVar(value="")
        self._settings = get_ui_settings()

        self.grid_propagate(False)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.rail = ttk.Frame(self, padding=(4, 6))
        self.rail.grid(row=0, column=0, sticky="ns")

        self.toggle_btn = ttk.Button(self.rail, text=">", width=3, command=self.toggle)
        self.toggle_btn.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(self.rail, text="Stat", width=3, command=lambda: self.expand("Status")).pack(fill=tk.X, pady=3)
        ttk.Button(self.rail, text="Set", width=3, command=lambda: self.expand("Settings")).pack(fill=tk.X, pady=3)

        self.body = ttk.Frame(self, padding=(4, 6))
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self.body)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.status_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.status_tab, text="Status")
        self.notebook.add(self.settings_tab, text="Settings")

        self._build_status_tab()
        self._build_settings_tab()
        self.collapse()
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_refresh()

    def _build_status_tab(self):
        scale = ui_scale_for_widget(self)
        body_wrap = scaled_wrap(250, scale)
        self.status_tab.columnconfigure(0, weight=1)

        ttk.Label(self.status_tab, text="Mode", font=scaled_font(10, "bold", scale=scale)).grid(row=0, column=0, sticky="w")
        ttk.Label(self.status_tab, text=self.mode_name).grid(row=1, column=0, sticky="w", pady=(0, 10))

        ttk.Button(
            self.status_tab,
            text=self.switch_label,
            command=lambda: self.app.request_mode_switch(self._target_mode()),
        ).grid(row=2, column=0, sticky="ew", pady=(0, 14))

        ttk.Label(self.status_tab, text="Message", font=scaled_font(10, "bold", scale=scale)).grid(row=3, column=0, sticky="w")
        ttk.Label(
            self.status_tab,
            textvariable=self.status_var,
            wraplength=body_wrap,
            justify=tk.LEFT,
        ).grid(row=4, column=0, sticky="ew", pady=(0, 14))

        ttk.Label(self.status_tab, text="Summary", font=scaled_font(10, "bold", scale=scale)).grid(row=5, column=0, sticky="w")
        ttk.Label(
            self.status_tab,
            textvariable=self.summary_var,
            wraplength=body_wrap,
            justify=tk.LEFT,
        ).grid(row=6, column=0, sticky="ew", pady=(0, 14))

        if self.mode_name == "Acquisition":
            ttk.Label(self.status_tab, text="Spectrometer", font=scaled_font(10, "bold", scale=scale)).grid(row=7, column=0, sticky="w")
            ttk.Label(
                self.status_tab,
                textvariable=self.spectrometer_var,
                wraplength=body_wrap,
                justify=tk.LEFT,
            ).grid(row=8, column=0, sticky="ew")

    def _build_settings_tab(self):
        scale = ui_scale_for_widget(self)
        body_wrap = scaled_wrap(250, scale)
        self.settings_tab.columnconfigure(1, weight=1)

        ttk.Label(self.settings_tab, text="Theme:").grid(row=0, column=0, sticky="w", pady=4)
        self.theme_var = tk.StringVar(value=self._settings["theme"])
        theme_combo = ttk.Combobox(
            self.settings_tab,
            textvariable=self.theme_var,
            values=["Light", "Dark"],
            state="readonly",
            width=12,
        )
        theme_combo.grid(row=0, column=1, sticky="ew", pady=4)
        theme_combo.bind("<<ComboboxSelected>>", lambda _event: self._save_ui_settings(apply_theme=True))

        ttk.Label(self.settings_tab, text="Export DPI:").grid(row=1, column=0, sticky="w", pady=4)
        self.export_dpi_var = tk.StringVar(value=str(self._settings["default_export_dpi"]))
        dpi_spin = ttk.Spinbox(
            self.settings_tab,
            from_=72,
            to=1200,
            increment=25,
            textvariable=self.export_dpi_var,
            width=8,
            command=self._save_ui_settings,
        )
        dpi_spin.grid(row=1, column=1, sticky="w", pady=4)
        dpi_spin.bind("<FocusOut>", lambda _event: self._save_ui_settings())
        dpi_spin.bind("<Return>", lambda _event: self._save_ui_settings())

        self.confirm_clean_var = tk.BooleanVar(value=self._settings["confirm_clean_reset"])
        ttk.Checkbutton(
            self.settings_tab,
            text="Confirm clean/reset actions",
            variable=self.confirm_clean_var,
            command=self._save_ui_settings,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 4))

        ttk.Label(
            self.settings_tab,
            text="Settings save automatically.",
            foreground="gray",
            wraplength=body_wrap,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _target_mode(self) -> str:
        return "Acquisition" if self.mode_name == "Analysis" else "Analysis"

    def _save_ui_settings(self, apply_theme: bool = False):
        try:
            export_dpi = max(1, int(self.export_dpi_var.get()))
        except ValueError:
            export_dpi = self._settings["default_export_dpi"]
            self.export_dpi_var.set(str(export_dpi))

        self._settings = {
            "theme": self.theme_var.get(),
            "default_export_dpi": export_dpi,
            "confirm_clean_reset": self.confirm_clean_var.get(),
        }
        save_ui_settings(self._settings)
        if apply_theme:
            try:
                import sv_ttk

                sv_ttk.set_theme(self.theme_var.get().lower())
            except Exception:
                pass
            apply_text_scale(self.winfo_toplevel())
            apply_theme_hook = getattr(self.app, "apply_ui_theme", None)
            if callable(apply_theme_hook):
                apply_theme_hook()

    def expand(self, tab_name: str | None = None):
        self.expanded = True
        self.configure(width=EXPANDED_WIDTH)
        self.body.grid(row=0, column=1, sticky="nsew")
        self.toggle_btn.config(text="<")
        if tab_name:
            index = 0 if tab_name == "Status" else 1
            self.notebook.select(index)

    def collapse(self):
        self.expanded = False
        self.body.grid_remove()
        self.configure(width=COLLAPSED_WIDTH)
        self.toggle_btn.config(text=">")

    def toggle(self):
        if self.expanded:
            self.collapse()
        else:
            self.expand()

    def _schedule_refresh(self):
        self._refresh_status()
        self._refresh_after_id = self.after(750, self._schedule_refresh)

    def _refresh_status(self):
        try:
            snapshot = self.app.get_status_snapshot()
        except Exception:
            snapshot = {}
        self.summary_var.set(snapshot.get("summary", ""))
        self.spectrometer_var.set(snapshot.get("spectrometer_summary", ""))

    def _on_destroy(self, _event=None):
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None


def attach_right_inspector(root, app, *, mode_name: str, status_var: tk.StringVar, switch_label: str) -> RightInspector:
    """Create and grid a collapsed right inspector in root column 2."""
    root.grid_columnconfigure(2, minsize=COLLAPSED_WIDTH, weight=0)
    inspector = RightInspector(root, app, mode_name=mode_name, status_var=status_var, switch_label=switch_label)
    inspector.grid(row=0, column=2, sticky="ns")
    return inspector
