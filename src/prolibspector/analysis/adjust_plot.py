"""Plot adjustment dialog and helper logic for Analysis Mode."""

import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
from tkinter.colorchooser import askcolor
import matplotlib.colors as mcolors
from tkinter import messagebox
import numpy as np
try:
    import markdown
except ImportError:
    markdown = None

try:
    from tkhtmlview import HTMLLabel
except ImportError:
    HTMLLabel = None
from prolibspector.core.settings import capture_plot_settings, load_settings, save_settings
from prolibspector.core.ui_theme import get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog, make_markdown_help_window



#=======================================================================================================================
# Normalization functions
def normalize_min_max(y_data):
    """Min-Max normalization: maps values to [0, 1]."""
    y = np.asarray(y_data, dtype=float)
    y_min, y_max = y.min(), y.max()
    if y_max == y_min:
        return np.zeros_like(y)
    return (y - y_min) / (y_max - y_min)


def normalize_total_intensity(y_data, eps=1e-12, clip_negative=True):
    """
    Total-intensity (area) normalization: y / sum(y).
    Assumes baseline correction was already done.
    Clips negatives to 0 by default to avoid sum distortion.
    """
    y = np.asarray(y_data, dtype=float)
    if clip_negative:
        y = np.clip(y, 0, None)
    s = np.sum(y)
    return y / (s + eps)


def normalize_snv(y_data):
    """
    Standard Normal Variate (SNV) transformation: (y - mean(y)) / std(y).

    Centres the spectrum on zero and scales it to unit standard deviation, which
    removes multiplicative scaling differences between shots (varying pulse
    energy, coupling efficiency, or detector gain) without needing a reference
    line. The result is dimensionless and contains negative values, so it is a
    comparison aid rather than an intensity measurement.

    A completely flat trace has zero variance and no meaningful SNV; it is
    returned as all zeros rather than dividing by zero.
    """
    y = np.asarray(y_data, dtype=float)
    std = y.std()
    if std == 0:
        return np.zeros_like(y)
    return (y - y.mean()) / std


# Main function for adjusting the plot
def adjust_plot(app, ax):

    if not app.line:
        messagebox.showerror("Error", "Please import data before adjusting the plot.", parent=app.root)
        return

    # Store original y_data so normalization can be reversed
    if not hasattr(app, '_original_y_data') or app._original_y_data is None:
        app._original_y_data = app.y_data.copy()

    def update_plot():
        method = normalize_var.get()
        if method == "Min-Max":
            y_data = normalize_min_max(app._original_y_data)
            app.y_data = y_data
            app.line.set_ydata(y_data)
            ax.set_ylim(0, 1)
        elif method == "Total Intensity":
            y_data = normalize_total_intensity(app._original_y_data)
            app.y_data = y_data
            app.line.set_ydata(y_data)
            ax.set_ylim(0, y_data.max() * 1.05)
        elif method == "SNV":
            y_data = normalize_snv(app._original_y_data)
            app.y_data = y_data
            app.line.set_ydata(y_data)
            # SNV is zero-centred, so the lower bound must follow the data
            # rather than being pinned to zero.
            low, high = float(y_data.min()), float(y_data.max())
            margin = max((high - low) * 0.05, 1e-6)
            ax.set_ylim(low - margin, high + margin)
        else:
            # "None" – restore original data
            app.y_data = app._original_y_data.copy()
            app.line.set_ydata(app.y_data)
            ax.set_ylim(y_start.get(), y_end.get())

        # Get the currently selected line object from the app class
        line = app.line
        # Update the line object properties
        line.set_color(line_color.get())
        line.set_linewidth(line_width.get())
        # Update the axis limits and background color
        ax.set_xlim(x_start.get(), x_end.get())
        ax.set_facecolor(mcolors.to_rgba(bg_color.get()))
        # Redraw the canvas to show the updated plot
        app.canvas.draw()

    def update_line_color(app):
        color = askcolor(parent=adjust_window)[1]
        if color:
            line_color.set(color)

    def update_bg_color(app):
        color = askcolor(parent=adjust_window)[1]
        if color:
            bg_color.set(color)

    #=======================================================================================================================
    # Create a new top level window
    adjust_window = create_dialog(app.root, "Adjust Plot", resizable=True)

    # Create a notebook (tabbed window)
    notebook = ttk.Notebook(adjust_window)
    notebook.pack(fill="both", expand=True, padx=10, pady=10)

    # Create the first tab for axis adjustment
    tab1 = ttk.Frame(notebook)
    notebook.add(tab1, text="Adjust Axis")

    # Add the normalization method selector
    ttk.Label(tab1, text="Normalization:").grid(row=2, column=0, padx=(20, 5), pady=(10, 10), sticky="e")
    normalize_var = tk.StringVar(value="None")
    normalize_combo = ttk.Combobox(tab1, textvariable=normalize_var, state="readonly", width=18,
                                   values=["None", "Min-Max", "Total Intensity", "SNV"])
    normalize_combo.grid(row=2, column=1, padx=(5, 20), pady=(10, 10), sticky="w")

    # Create variables to store the axis limits
    x_start = tk.DoubleVar(value=ax.get_xlim()[0])
    x_end = tk.DoubleVar(value=ax.get_xlim()[1])
    y_start = tk.DoubleVar(value=round(ax.get_ylim()[0], 1))
    y_end = tk.DoubleVar(value=round(ax.get_ylim()[1], 1))

    # Create the widgets for the first tab
    ttk.Label(tab1, text="Wavelength (X-axis): From").grid(row=0, column=0, padx=(16, 8), pady=(16, 8), sticky="e")
    x_start_entry = ttk.Entry(tab1, textvariable=x_start, width=20)
    x_start_entry.grid(row=0, column=1, padx=8, pady=(16, 8))
    ttk.Label(tab1, text="to").grid(row=0, column=2, padx=8, pady=(16, 8))
    ttk.Entry(tab1, textvariable=x_end, width=20).grid(row=0, column=3, padx=(8, 16), pady=(16, 8))
    x_start_entry.focus_set()

    # Create the widgets for the second tab
    ttk.Label(tab1, text="Relative Intensity (Y-axis): From").grid(row=1, column=0, padx=(16, 8), pady=8, sticky="e")
    ttk.Entry(tab1, textvariable=y_start, width=20).grid(row=1, column=1, padx=8, pady=8)
    ttk.Label(tab1, text="to").grid(row=1, column=2, padx=8, pady=8)
    ttk.Entry(tab1, textvariable=y_end, width=20).grid(row=1, column=3, padx=(8, 16), pady=8)

    # Create the second tab for visual adjustments
    tab2 = ttk.Frame(notebook)
    notebook.add(tab2, text="Visual Adjustments")

    # Read actual values from the current plot line
    current_line = ax.lines[-1] if ax.lines else None
    if current_line:
        raw_color = current_line.get_color()
        initial_line_color = mcolors.to_hex(raw_color).upper()
        initial_line_width = current_line.get_linewidth()
    else:
        initial_line_color = get_palette()["spectrum_line"]
        initial_line_width = 1.0

    line_color = tk.StringVar(value=initial_line_color)
    bg_color = tk.StringVar(value=mcolors.to_hex(ax.get_facecolor()).upper())
    line_width = tk.DoubleVar(value=initial_line_width)

    ttk.Label(tab2, text="Line Color:").grid(row=0, column=0, padx=(16, 8), pady=(16, 8), sticky="e")
    ttk.Button(tab2, text="Select Color", command=lambda: update_line_color(app)).grid(row=0, column=1, padx=8, pady=(16, 8), sticky="w")

    ttk.Label(tab2, text="Background Color:").grid(row=1, column=0, padx=(16, 8), pady=8, sticky="e")
    ttk.Button(tab2, text="Select Color", command=lambda: update_bg_color(app)).grid(row=1, column=1, padx=8, pady=8, sticky="w")

    ttk.Label(tab2, text="Line Width:").grid(row=2, column=0, padx=(16, 8), pady=8, sticky="e")
    line_width_scale = ttk.Scale(
        tab2,
        from_=1,
        to=10,
        orient="horizontal",
        variable=line_width,
        length=300,
        command=lambda val: line_width_readout.config(text=f"{float(val):.1f} px"),
    )
    line_width_scale.grid(row=2, column=1, padx=8, pady=8, sticky="w")
    line_width_readout = ttk.Label(tab2, text=f"{line_width.get():.1f} px")
    line_width_readout.grid(row=2, column=2, padx=(8, 16), pady=8, sticky="w")


#=======================================================================================================================   
    # Create a frame
    button_frame = ttk.Frame(adjust_window)
    button_frame.pack(pady=20)

    # Create an "Apply" button to apply the changes
    def on_apply():
        try:
            x_min = x_start.get()
            x_max = x_end.get()
            y_min = y_start.get()
            y_max = y_end.get()
        except tk.TclError:
            messagebox.showwarning(
                "Invalid Plot Settings",
                "Enter valid numeric axis limits.",
                parent=adjust_window,
            )
            return

        if x_min >= x_max:
            messagebox.showwarning(
                "Invalid Plot Settings",
                "The X-axis start must be less than the X-axis end.",
                parent=adjust_window,
            )
            return
        if normalize_var.get() == "None" and y_min >= y_max:
            messagebox.showwarning(
                "Invalid Plot Settings",
                "The Y-axis start must be less than the Y-axis end.",
                parent=adjust_window,
            )
            return

        update_plot()
        # Save plot settings to presets
        plot_settings = capture_plot_settings(x_start, x_end, y_start, y_end,
                                             line_color, bg_color, line_width, normalize_var)
        settings = load_settings()
        if settings is None:
            settings = {"adjust_spectrum": {}, "adjust_plot": {}}
        settings["adjust_plot"] = plot_settings
        save_settings(settings)
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status(f"Applied plot settings ({normalize_var.get()} normalization).")
        adjust_window.destroy()
    
    apply_button = ttk.Button(button_frame, text="Apply", command=on_apply, style="Accent.TButton")
    apply_button.pack(side="left", padx=10)

    # Add the Help button
    help_button = ttk.Button(button_frame, text="Help", command=open_help_document)
    help_button.pack(side="right", padx=10)

    adjust_window.bind("<Return>", lambda _event: on_apply())
    apply_window_policy(
        adjust_window,
        parent=app.root,
        preferred=(720, 430),
        min_size=(520, 320),
        modal=False,
        resizable=True,
        remember_key="adjust_plot",
    )

#=======================================================================================================================   




def open_help_document():
    # Define your markdown text
    markdown_text = """
# Help Section: Adjust Plot

Welcome to the Adjust Plot help section. This section aims to guide you on how to use the features available in the Adjust Plot window.

**Tab 1: Axis Adjustment and Normalization**

In this tab, you can adjust the X-axis (wavelength) and Y-axis (relative intensity) of the plot.

**X-Axis (Wavelength)**

The X-axis of the plot represents the wavelength. In Laser-Induced Breakdown Spectroscopy (LIBS), each element in the sample produces a unique set of emission lines (wavelengths), allowing for element identification.

To adjust the X-axis of the plot, simply enter your desired minimum and maximum wavelengths in the appropriate fields and click apply.

**Y-Axis (Relative Intensity)**

The Y-axis of the plot represents the relative intensity of the emission. In LIBS, the intensity of the emission lines correlates with the abundance of the corresponding elements in the sample.

To adjust the Y-axis of the plot, enter your desired minimum and maximum relative intensities in the appropriate fields and click apply.

**Normalization**

Normalization is a crucial step in data preprocessing and analysis, especially in the context of Laser-Induced Breakdown Spectroscopy (LIBS). It is a process that adjusts the measured values from different scales to a common scale.

In the context of your application, normalization rescales the relative intensity values (Y-axis) of your spectral data so that spectra recorded under different conditions become comparable. Three methods are available:

-   **Min-Max**: maps the spectrum onto the range 0 to 1 using its own minimum and maximum.
-   **Total Intensity**: divides by the summed intensity (area), so the spectrum describes how the signal is distributed across wavelengths.
-   **SNV** (Standard Normal Variate): subtracts the mean and divides by the standard deviation. This removes shot-to-shot multiplicative differences such as varying pulse energy or detector gain without needing a reference line. SNV output is zero-centred and dimensionless, so it contains negative values and is a comparison aid rather than an intensity measurement.

The purpose of normalization in LIBS data is multi-fold:

1.  **Easier Comparison:** Normalization allows for easier comparison between different elements or different samples. For instance, when you are comparing the LIBS spectra of two different samples, normalization can help you compare the relative intensities of spectral lines across the two samples more effectively.
2.  **Mitigate Influence of Extreme Values:** Without normalization, extreme values or outliers in your data can significantly influence your results. Normalization helps to mitigate this by bringing all values within a standard range.
3.  **Aiding in Visualization:** Normalized data can be more straightforward to visualize and interpret in a plot because all data will be in the same scale, making it easier to identify patterns and trends.
4.  **Improving Analytical Techniques:** Certain analytical techniques or algorithms (if applied to the data) require the data to be normalized to function correctly.

**Tab 2: Visual Adjustments**

This tab allows you to make visual adjustments to the plot.

**Line Color and Width**

You can adjust the color and width of the plot line. These adjustments help with the visualization of the plot, particularly when multiple lines are present or when presenting the plot.

**Plot Background Color**

You can also adjust the plot's background color. A contrasting background color can help make the plot lines stand out more clearly."""

# Convert the markdown to HTML
    html_text = markdown.markdown(markdown_text)

    make_markdown_help_window(
        tk._default_root,
        "Adjust Plot Help",
        lambda: html_text,
        lambda parent, html: HTMLLabel(parent, html=html),
    )

