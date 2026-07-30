"""Sidebar controls for Acquisition Mode."""

import os
import tkinter as tk
from tkinter import ttk

from prolibspector.app.launcher import MODE_CHOOSER
from prolibspector.core.icons import configure_themed_icon
from prolibspector.core.ui_scale import scaled_font, scaled_int, scaled_padding, scaled_wrap, ui_scale_for_widget
from prolibspector.core.settings import default_save_directory, get_ui_pref
from prolibspector.core.tooltip import attach_tooltip
from prolibspector.core.ui_theme import get_palette
from prolibspector.hardware.spectrometer import DEFAULT_INTEGRATION_TIME_US


ACQUISITION_SIDEBAR_WIDTH = 360
_SIDEBAR_SECTION_PAD_X = 18
_SIDEBAR_TEXT_WRAP = ACQUISITION_SIDEBAR_WIDTH - (_SIDEBAR_SECTION_PAD_X * 2) - 28


def _install_mousewheel_scrolling(canvas: tk.Canvas, *widgets):
    """Scroll the sidebar while the pointer is over it."""
    hover_depth = 0
    wheel_accum = 0.0

    def _on_mousewheel(event):
        nonlocal wheel_accum
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
        elif event.delta:
            # Accumulate fractional deltas so touchpads (|delta| < 120) still scroll.
            wheel_accum += -event.delta / 120.0
            units = int(wheel_accum)
            if units:
                wheel_accum -= units
                canvas.yview_scroll(units, "units")
        return "break"

    def _bind(_event):
        nonlocal hover_depth
        hover_depth += 1
        if hover_depth == 1:
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind(_event):
        nonlocal hover_depth
        hover_depth = max(0, hover_depth - 1)

        def _maybe_unbind():
            if hover_depth == 0:
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")

        canvas.after_idle(_maybe_unbind)

    for widget in widgets:
        widget.bind("<Enter>", _bind, add="+")
        widget.bind("<Leave>", _unbind, add="+")

    return _on_mousewheel


def _shield_wheel_value_changes(container, wheel_handler):
    """Keep the wheel scrolling the sidebar instead of mutating values.

    Comboboxes and spinboxes change their value on MouseWheel via their
    class binding, which fires before any bind_all handler — so scrolling
    the sidebar across one could silently change a setting. A widget-level
    binding runs first and redirects the event to the sidebar scroll."""
    for child in container.winfo_children():
        if isinstance(child, (ttk.Combobox, ttk.Spinbox)):
            child.bind("<MouseWheel>", wheel_handler)
        _shield_wheel_value_changes(child, wheel_handler)


def _configure_app_icon(app, widget, owner_attr: str, icon_name: str, size: tuple[int, int]) -> None:
    setattr(app, owner_attr, None)
    try:
        configure_themed_icon(widget, icon_name, size, owner=app, owner_attr=owner_attr)
    except Exception:
        pass


def build_advanced_options_widgets(app, parent, *, sidebar_wrap):
    """Create the shared spectrometer runtime option widgets inside ``parent``.

    Used by both the manual and automated sidebars so integration time,
    averages, and correction controls stay a single implementation wired to
    the app-level handlers (``on_apply_integration``, ``on_averages_changed``,
    ``on_corrections_changed``). Tk variables already present on ``app`` are
    reused instead of being recreated.
    """
    parent.columnconfigure(1, weight=1)

    if getattr(app, "integration_var", None) is None:
        app.integration_var = tk.StringVar(
            value=get_ui_pref("last_integration_ms", f"{DEFAULT_INTEGRATION_TIME_US / 1000:g}")
        )
    ttk.Label(parent, text="Integration:", style="Status.TLabel").grid(
        row=0, column=0, padx=6, pady=4, sticky="w",
    )
    app.integration_entry = ttk.Entry(
        parent, textvariable=app.integration_var, width=8, style="Sidebar.TEntry"
    )
    app.integration_entry.grid(row=0, column=1, padx=(0, 4), pady=4, sticky="ew")
    app.integration_entry.bind("<Return>", lambda _event: app.on_apply_integration())

    ttk.Label(parent, text="ms", style="Status.TLabel").grid(
        row=0, column=2, padx=2, pady=4, sticky="w",
    )

    app.apply_int_btn = ttk.Button(
        parent,
        text="Apply",
        command=app.on_apply_integration,
        style="Sidebar.TButton",
        state="disabled",
    )
    app.apply_int_btn.grid(row=0, column=3, padx=(6, 6), pady=4, sticky="e")

    ttk.Label(parent, text="Averages:", style="Status.TLabel").grid(
        row=1, column=0, padx=6, pady=4, sticky="w",
    )
    if getattr(app, "averages_var", None) is None:
        app.averages_var = tk.StringVar(value="1")
    app.averages_spinbox = ttk.Spinbox(
        parent,
        from_=1,
        to=100,
        width=6,
        textvariable=app.averages_var,
        command=app.on_averages_changed,
        style="Sidebar.TSpinbox",
    )
    app.averages_spinbox.grid(row=1, column=1, columnspan=3, padx=(0, 6), pady=4, sticky="w")

    if getattr(app, "correct_dark_var", None) is None:
        app.correct_dark_var = tk.BooleanVar(value=True)
    app.dark_check = ttk.Checkbutton(
        parent,
        text="Dark count correction",
        variable=app.correct_dark_var,
        command=app.on_corrections_changed,
        style="Sidebar.TCheckbutton",
    )
    app.dark_check.grid(row=2, column=0, columnspan=4, padx=6, pady=2, sticky="w")

    if getattr(app, "correct_nl_var", None) is None:
        app.correct_nl_var = tk.BooleanVar(value=True)
    app.nl_check = ttk.Checkbutton(
        parent,
        text="Nonlinearity correction",
        variable=app.correct_nl_var,
        command=app.on_corrections_changed,
        style="Sidebar.TCheckbutton",
    )
    app.nl_check.grid(row=3, column=0, columnspan=4, padx=6, pady=2, sticky="w")

    if getattr(app, "hold_y_axis_var", None) is None:
        app.hold_y_axis_var = tk.BooleanVar(value=False)

    def _on_hold_y_changed():
        if hasattr(app, "ax"):
            app.ax._hold_y_autoscale = bool(app.hold_y_axis_var.get())

    app.hold_y_check = ttk.Checkbutton(
        parent,
        text="Hold Y-axis scale",
        variable=app.hold_y_axis_var,
        command=_on_hold_y_changed,
        style="Sidebar.TCheckbutton",
    )
    app.hold_y_check.grid(row=4, column=0, columnspan=4, padx=6, pady=2, sticky="w")
    attach_tooltip(
        app.hold_y_check,
        "Freeze the current Y range during live view instead of auto-zooming "
        "with the signal.",
    )

    if getattr(app, "int_range_var", None) is None:
        app.int_range_var = tk.StringVar(value="")
    app.int_range_label = ttk.Label(
        parent,
        textvariable=app.int_range_var,
        style="Muted.TLabel",
        wraplength=sidebar_wrap,
    )
    app.int_range_label.grid(row=5, column=0, columnspan=4, padx=6, pady=(0, 4), sticky="ew")


def create_acquisition_sidebar(app):
    """
    Build the sidebar for Acquisition Mode with spectrometer controls.

    Args:
        app: The AcquisitionApp instance.
    """
    scale = ui_scale_for_widget(app.root)
    sidebar_wrap = scaled_wrap(_SIDEBAR_TEXT_WRAP, scale)
    style = ttk.Style()
    style.configure("Sidebar.TLabelframe.Label", font=scaled_font(10, "bold", scale=scale))
    style.configure("LeftAligned.TButton", anchor="w", font=scaled_font(9, scale=scale))
    style.configure("Sidebar.TButton", font=scaled_font(9, scale=scale))
    style.configure("Sidebar.TCheckbutton", font=scaled_font(9, scale=scale))
    style.configure("Sidebar.TEntry", font=scaled_font(9, scale=scale))
    style.configure("Sidebar.TSpinbox", font=scaled_font(9, scale=scale))
    style.configure("Emphasized.TLabel", font=scaled_font(16, "bold", scale=scale))
    style.configure("Status.TLabel", font=scaled_font(9, scale=scale))
    style.configure("StatusValue.TLabel", font=scaled_font(9, "bold", scale=scale))
    style.configure("Muted.TLabel", font=scaled_font(9, scale=scale), foreground=get_palette()["muted_text"])

    app.sidebar_outer = ttk.Frame(app.root)
    app.sidebar_outer.grid(row=0, column=0, sticky="nsew")
    app.sidebar_outer.grid_propagate(False)

    canvas_bg = app.root.cget("bg")
    app.sidebar_canvas = tk.Canvas(
        app.sidebar_outer,
        highlightthickness=0,
        borderwidth=0,
        bg=canvas_bg,
    )
    app.sidebar_scrollbar = ttk.Scrollbar(
        app.sidebar_outer,
        orient=tk.VERTICAL,
        command=app.sidebar_canvas.yview,
    )
    app.sidebar_canvas.configure(yscrollcommand=app.sidebar_scrollbar.set)
    app.sidebar_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    app.sidebar_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    app.sidebar_frame = ttk.Frame(app.sidebar_canvas)
    app.sidebar_frame.columnconfigure(0, weight=1)
    sidebar_window = app.sidebar_canvas.create_window((0, 0), window=app.sidebar_frame, anchor="nw")

    def _refresh_sidebar_scrollregion(_event=None):
        app.sidebar_canvas.configure(scrollregion=app.sidebar_canvas.bbox("all"))

    def _resize_sidebar_content(event):
        app.sidebar_canvas.itemconfigure(sidebar_window, width=event.width)

    app.sidebar_frame.bind("<Configure>", _refresh_sidebar_scrollregion)
    app.sidebar_canvas.bind("<Configure>", _resize_sidebar_content)
    app._sidebar_wheel_handler = _install_mousewheel_scrolling(
        app.sidebar_canvas, app.sidebar_outer, app.sidebar_canvas, app.sidebar_frame
    )
    app._refresh_sidebar_scrollregion = _refresh_sidebar_scrollregion

    icon_size = (32, 32)

    app.mode_menu_btn = ttk.Button(
        app.sidebar_frame,
        text="< Mode Menu",
        style="Sidebar.TButton",
        command=lambda: app.request_mode_switch(MODE_CHOOSER),
    )
    app.mode_menu_btn.grid(row=0, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=(12, 0), sticky="w")

    conn_frame = ttk.LabelFrame(app.sidebar_frame, text="Spectrometer", padding=6, style="Sidebar.TLabelframe")
    app.spectrometer_frame = conn_frame
    conn_frame.grid(row=1, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=(10, 6), sticky="ew")
    conn_frame.columnconfigure(0, weight=1)

    app._connect_icon = None

    app.connect_btn = ttk.Button(
        conn_frame,
        text="Connect",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_connect,
    )
    _configure_app_icon(app, app.connect_btn, "_connect_icon", "Import_icon.png", icon_size)
    app.connect_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    app.disconnect_btn = ttk.Button(
        conn_frame,
        text="Disconnect",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_disconnect,
        state="disabled",
    )
    app.disconnect_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    app.diagnose_btn = ttk.Button(
        conn_frame,
        text="Diagnose...",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_diagnose,
    )
    app.diagnose_btn.grid(row=2, column=0, padx=6, pady=4, sticky="ew")

    app.connection_status_var = tk.StringVar(value="Disconnected")
    status_label = ttk.Label(
        conn_frame,
        textvariable=app.connection_status_var,
        style="Muted.TLabel",
        wraplength=sidebar_wrap,
    )
    app.connection_status_label = status_label
    status_label.grid(row=3, column=0, padx=6, pady=(2, 6), sticky="ew")

    acq_frame = ttk.LabelFrame(app.sidebar_frame, text="Acquisition", padding=6, style="Sidebar.TLabelframe")
    app.acquisition_frame = acq_frame
    acq_frame.grid(row=2, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    acq_frame.columnconfigure(0, weight=1)

    app._live_icon = None

    app.live_btn = ttk.Button(
        acq_frame,
        text="Live View",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_live_view,
        state="disabled",
    )
    _configure_app_icon(app, app.live_btn, "_live_icon", "spectrum_icon.png", icon_size)
    app.live_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    app._arm_icon = None

    app.arm_btn = ttk.Button(
        acq_frame,
        text="Arm Trigger",
        compound="left",
        style="Accent.TButton",
        command=app.on_arm_trigger,
        state="disabled",
    )
    _configure_app_icon(app, app.arm_btn, "_arm_icon", "trigger_icon.png", icon_size)
    app.arm_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    app.arm_status_var = tk.StringVar(value="Trigger unavailable")
    _chip = get_palette()
    app.arm_status_chip = tk.Label(
        acq_frame,
        textvariable=app.arm_status_var,
        bg=_chip["well_disabled"],
        fg=_chip["muted_text"],
        font=scaled_font(9, "bold", scale=scale),
        padx=scaled_int(10, scale),
        pady=scaled_int(4, scale),
        anchor="w",
        wraplength=sidebar_wrap,
    )
    app.arm_status_chip.grid(row=2, column=0, padx=6, pady=(0, 4), sticky="ew")

    app.test_trigger_btn = ttk.Button(
        acq_frame,
        text="Test Trigger",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_test_trigger,
        state="disabled",
    )
    app.test_trigger_btn.grid(row=3, column=0, padx=6, pady=4, sticky="ew")

    app._stop_icon = None

    app.stop_btn = ttk.Button(
        acq_frame,
        text="Stop",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_stop,
        state="disabled",
    )
    _configure_app_icon(app, app.stop_btn, "_stop_icon", "clean_icon.png", icon_size)
    app.stop_btn.grid(row=4, column=0, padx=6, pady=4, sticky="ew")

    int_frame = ttk.LabelFrame(app.sidebar_frame, text="Advanced Options", padding=6, style="Sidebar.TLabelframe")
    app.advanced_options_frame = int_frame
    int_frame.grid(row=3, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    int_frame.columnconfigure(0, weight=1)

    app.advanced_options_expanded = tk.BooleanVar(value=False)
    app.advanced_options_label_var = tk.StringVar()
    app.advanced_options_body = ttk.Frame(int_frame)

    build_advanced_options_widgets(app, app.advanced_options_body, sidebar_wrap=sidebar_wrap)

    def _set_advanced_options_label(*_):
        arrow = "v" if app.advanced_options_expanded.get() else ">"
        app.advanced_options_label_var.set(
            f"Integration: {app.integration_var.get()} ms {arrow}"
        )

    def _toggle_advanced_options():
        app.advanced_options_expanded.set(not app.advanced_options_expanded.get())
        if app.advanced_options_expanded.get():
            app.advanced_options_body.grid(row=1, column=0, sticky="ew")
        else:
            app.advanced_options_body.grid_remove()
        _set_advanced_options_label()
        app._post_to_ui(app._refresh_sidebar_scrollregion)

    app.integration_var.trace_add("write", _set_advanced_options_label)
    _set_advanced_options_label()

    app.advanced_options_btn = ttk.Button(
        int_frame,
        textvariable=app.advanced_options_label_var,
        command=_toggle_advanced_options,
        style="LeftAligned.TButton",
    )
    app.advanced_options_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")
    app.advanced_options_body.grid(row=1, column=0, sticky="ew")
    app.advanced_options_body.grid_remove()

    save_frame = ttk.LabelFrame(app.sidebar_frame, text="Auto-Save", padding=6, style="Sidebar.TLabelframe")
    app.save_frame = save_frame
    save_frame.grid(row=4, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    save_frame.columnconfigure(0, weight=0)
    save_frame.columnconfigure(1, weight=1)

    app.auto_save_var = tk.BooleanVar(value=True)
    auto_save_check = ttk.Checkbutton(
        save_frame,
        text="Auto-save on trigger",
        variable=app.auto_save_var,
        command=app.on_auto_save_toggle,
        style="Sidebar.TCheckbutton",
    )
    app.auto_save_check = auto_save_check
    auto_save_check.grid(row=0, column=0, columnspan=2, padx=6, pady=4, sticky="w")

    ttk.Label(save_frame, text="Experiment Name:", style="Status.TLabel").grid(
        row=1,
        column=0,
        padx=6,
        pady=4,
        sticky="w",
    )
    app.sample_name_var = tk.StringVar(value=get_ui_pref("last_sample_name", "Sample"))
    app.sample_entry = ttk.Entry(save_frame, textvariable=app.sample_name_var, style="Sidebar.TEntry")
    app.sample_entry.grid(row=1, column=1, padx=6, pady=4, sticky="ew")
    app.sample_name_var.trace_add("write", lambda *_: app.on_sample_name_changed())

    ttk.Label(save_frame, text="Save to:", style="Status.TLabel").grid(
        row=2,
        column=0,
        padx=6,
        pady=4,
        sticky="w",
    )
    app.save_dir_var = tk.StringVar(value=default_save_directory())

    app.browse_save_dir_btn = ttk.Button(save_frame, text="Browse...", command=app.on_browse_save_dir, style="Sidebar.TButton")
    app.browse_save_dir_btn.grid(row=2, column=1, padx=6, pady=4, sticky="ew")

    app.shot_count_var = tk.StringVar(value="Shots: 0")
    ttk.Label(
        save_frame,
        textvariable=app.shot_count_var,
        style="StatusValue.TLabel",
    ).grid(row=3, column=0, columnspan=2, padx=6, pady=4, sticky="w")

    app.plate_mode_var = tk.BooleanVar(value=False)
    app.plate_mode_check = ttk.Checkbutton(
        save_frame,
        text="High-throughput plate mode",
        variable=app.plate_mode_var,
        command=app.on_plate_mode_toggle,
        style="Sidebar.TCheckbutton",
    )
    app.plate_mode_check.grid(row=4, column=0, columnspan=2, padx=6, pady=(8, 4), sticky="w")

    app.configure_plate_btn = ttk.Button(
        save_frame,
        text="Configure Plate...",
        command=app.on_configure_plate,
        style="Sidebar.TButton",
        state="disabled",
    )
    app.configure_plate_btn.grid(row=5, column=0, columnspan=2, padx=6, pady=4, sticky="ew")

    app.spectra_qc_enabled_var = tk.BooleanVar(value=True)
    app.spectra_qc_check = ttk.Checkbutton(
        save_frame,
        text="Run spectra QC after plate completes",
        variable=app.spectra_qc_enabled_var,
        command=app.on_spectra_qc_toggle,
        style="Sidebar.TCheckbutton",
        state="disabled",
    )
    app.spectra_qc_check.grid(row=6, column=0, columnspan=2, padx=6, pady=(6, 1), sticky="w")
    app.spectra_qc_status_var = tk.StringVar(value="Checking spectra QC calibration...")
    app.spectra_qc_status_label = ttk.Label(
        save_frame,
        textvariable=app.spectra_qc_status_var,
        style="Status.TLabel",
        wraplength=sidebar_wrap,
    )
    app.spectra_qc_status_label.grid(row=7, column=0, columnspan=2, padx=6, pady=(0, 3), sticky="ew")

    app.plate_progress_var = tk.StringVar(value="")
    app.plate_progress_label = ttk.Label(
        save_frame,
        textvariable=app.plate_progress_var,
        style="Status.TLabel",
        wraplength=sidebar_wrap,
    )
    app.plate_progress_label.grid(row=8, column=0, columnspan=2, padx=6, pady=2, sticky="ew")

    app.plate_actions_frame = ttk.Frame(save_frame)
    app.plate_actions_frame.grid(row=9, column=0, columnspan=2, padx=6, pady=(4, 6), sticky="ew")
    app.plate_actions_frame.columnconfigure(0, weight=1)
    app.plate_actions_frame.columnconfigure(1, weight=1)
    app.plate_actions_frame.columnconfigure(2, weight=1)

    app.discard_plate_shot_btn = ttk.Button(
        app.plate_actions_frame,
        text="Discard Last",
        command=app.on_discard_last_plate_shot,
        style="Sidebar.TButton",
        state="disabled",
    )
    app.discard_plate_shot_btn.grid(row=0, column=0, padx=(0, 3), sticky="ew")

    app.repair_plate_wells_btn = ttk.Button(
        app.plate_actions_frame,
        text="Repair",
        command=app.on_repair_plate_wells,
        style="Sidebar.TButton",
        state="disabled",
    )
    app.repair_plate_wells_btn.grid(row=0, column=1, padx=3, sticky="ew")

    app.finish_plate_btn = ttk.Button(
        app.plate_actions_frame,
        text="Finish Plate",
        command=app.on_finish_plate_early,
        style="Sidebar.TButton",
        state="disabled",
    )
    app.finish_plate_btn.grid(row=0, column=2, padx=(3, 0), sticky="ew")

    app.action_frame = ttk.LabelFrame(app.sidebar_frame, text="Actions", padding=6, style="Sidebar.TLabelframe")
    app.action_frame.grid(row=5, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    app.action_frame.columnconfigure(0, weight=1)

    app._send_icon = None

    app.send_to_analysis_btn = ttk.Button(
        app.action_frame,
        text="Send to Analysis",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_send_to_analysis,
        state="disabled",
    )
    _configure_app_icon(app, app.send_to_analysis_btn, "_send_icon", "export_icon.png", icon_size)
    app.send_to_analysis_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    app._save_icon = None

    app.save_spectrum_btn = ttk.Button(
        app.action_frame,
        text="Save Spectrum",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_save_spectrum,
        state="disabled",
    )
    _configure_app_icon(app, app.save_spectrum_btn, "_save_icon", "savedata_icon.png", icon_size)
    app.save_spectrum_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    app.status_frame = ttk.Frame(app.sidebar_frame, padding=6)
    app.status_frame.grid(row=6, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=(10, 12), sticky="ew")
    app.status_frame.columnconfigure(0, weight=1)

    app.worker_state_var = tk.StringVar(value="State: IDLE")
    app.worker_state_label = ttk.Label(
        app.status_frame,
        textvariable=app.worker_state_var,
        style="StatusValue.TLabel",
        foreground=get_palette()["qc_fail"],
    )
    app.worker_state_label.grid(row=0, column=0, padx=5, sticky="w")

    app.status_message_var = tk.StringVar(value="")
    ttk.Label(
        app.status_frame,
        textvariable=app.status_message_var,
        style="Status.TLabel",
        wraplength=sidebar_wrap,
    ).grid(row=1, column=0, padx=6, pady=2, sticky="ew")

    _shield_wheel_value_changes(app.sidebar_frame, app._sidebar_wheel_handler)
    app._post_to_ui(app._refresh_sidebar_scrollregion)
