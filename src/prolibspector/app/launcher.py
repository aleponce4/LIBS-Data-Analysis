"""Application launcher dialog for ProLIBSpector (Public Edition)."""

import tkinter as tk
from tkinter import ttk

from prolibspector.core.icons import configure_themed_icon
from prolibspector.core.runtime import set_window_icon
from prolibspector.core.ui_scale import scaled_font, scaled_int, scaled_padding, scaled_wrap, ui_scale_for_widget
from prolibspector.core.windowing import apply_window_policy, create_dialog

LAUNCHER_PREFERRED_SIZE = (720, 520)
LAUNCHER_MIN_SIZE = (640, 460)
LAUNCHER_DESCRIPTION_WRAP = 260
MODE_CHOOSER = "Mode Chooser"


class ModeLauncher:
    """Launcher dialog allowing mode selection (Analysis or Simulated Acquisition)."""

    def __init__(self, root):
        self.selected_mode = None
        self.root = root

        self.dialog = create_dialog(self.root, "ProLIBSpector Public Edition - Select Mode", resizable=True)
        self.dialog.withdraw()
        set_window_icon(self.dialog)
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        apply_window_policy(
            self.dialog,
            parent=self.root,
            preferred=LAUNCHER_PREFERRED_SIZE,
            min_size=LAUNCHER_MIN_SIZE,
            modal=False,
            resizable=True,
        )

    def _build_ui(self):
        scale = ui_scale_for_widget(self.dialog)
        main_frame = ttk.Frame(self.dialog, padding=scaled_int(30, scale))
        main_frame.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style()
        style.configure("Title.TLabel", font=scaled_font(20, "bold", scale=scale))
        style.configure("Subtitle.TLabel", font=scaled_font(11, scale=scale))
        style.configure("ModeTitle.TLabel", font=scaled_font(13, "bold", scale=scale))
        style.configure("ModeDesc.TLabel", font=scaled_font(9, scale=scale), wraplength=scaled_wrap(LAUNCHER_DESCRIPTION_WRAP, scale))

        title_label = ttk.Label(main_frame, text="ProLIBSpector (Public Edition)", style="Title.TLabel")
        title_label.pack(pady=(scaled_int(10, scale), scaled_int(2, scale)))

        subtitle_label = ttk.Label(
            main_frame,
            text="Scientific instrument-control and spectral analysis workbench",
            style="Subtitle.TLabel"
        )
        subtitle_label.pack(pady=(0, scaled_int(25, scale)))

        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.pack(fill=tk.X, expand=True)
        buttons_frame.columnconfigure(0, weight=1)
        buttons_frame.columnconfigure(1, weight=1)

        # --- Analysis Mode ---
        analysis_frame = ttk.LabelFrame(buttons_frame, text="", padding=scaled_int(20, scale))
        analysis_frame.grid(row=0, column=0, padx=scaled_int(15, scale), sticky="nsew")

        ttk.Label(analysis_frame, text="Analysis Mode", style="ModeTitle.TLabel").pack(pady=(0, scaled_int(5, scale)))
        ttk.Label(
            analysis_frame,
            text="Import, process, and analyze\nLIBS spectral data.\nBaseline correction, peak labeling,\nand NIST element lookup.",
            style="ModeDesc.TLabel",
            justify="center"
        ).pack(pady=(0, scaled_int(12, scale)))

        analysis_btn = ttk.Button(
            analysis_frame,
            text="Open Analysis",
            command=lambda: self._select_mode("Analysis"),
            width=18
        )
        analysis_btn.pack(pady=(0, scaled_int(5, scale)))

        # --- Acquisition Mode (Simulated) ---
        acquisition_frame = ttk.LabelFrame(buttons_frame, text="", padding=scaled_int(20, scale))
        acquisition_frame.grid(row=0, column=1, padx=scaled_int(15, scale), sticky="nsew")

        ttk.Label(acquisition_frame, text="Simulated Acquisition", style="ModeTitle.TLabel").pack(pady=(0, scaled_int(5, scale)))
        ttk.Label(
            acquisition_frame,
            text="Simulated LIBS spectrometer.\nLive acquisition, dark frame subtraction,\nand reproducible JSON manifests.",
            style="ModeDesc.TLabel",
            justify="center"
        ).pack(pady=(0, scaled_int(12, scale)))

        acquisition_btn = ttk.Button(
            acquisition_frame,
            text="Open Acquisition",
            command=lambda: self._select_mode("Acquisition"),
            width=18
        )
        acquisition_btn.pack(pady=(0, scaled_int(5, scale)))

        ttk.Label(
            main_frame,
            text="ProLIBSpector Public Edition",
            font=scaled_font(8, scale=scale),
            foreground="gray"
        ).pack(side=tk.BOTTOM, pady=(scaled_int(15, scale), 0))

    def _select_mode(self, mode):
        self.selected_mode = mode
        self.dialog.destroy()

    def _on_close(self):
        self.selected_mode = None
        self.dialog.destroy()

    def run(self):
        self.dialog.grab_set()
        self.root.wait_window(self.dialog)
        return self.selected_mode
