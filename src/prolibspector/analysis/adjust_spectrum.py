"""Spectrum adjustment dialog and helper logic for Analysis Mode."""

import logging
import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
from tkinter import messagebox
import numpy as np
from scipy.signal import savgol_filter, medfilt
from scipy.ndimage import gaussian_filter1d

try:
    import pywt
except ImportError:
    pywt = None

try:
    import markdown
except ImportError:
    markdown = None

try:
    from tkhtmlview import HTMLLabel
except ImportError:
    HTMLLabel = None

from prolibspector.core.tooltip import attach_tooltip
from prolibspector.core.windowing import apply_window_policy, create_dialog, make_markdown_help_window, make_scrollable_frame

logger = logging.getLogger(__name__)


# Module-level function for baseline removal - can be imported and used elsewhere
def apply_baseline_removal(y_data, lam=1e6, p=0.001, niter=10, clip=False):
    """
    Remove baseline using asymmetric least squares smoothing (ALS) - Eilers method.
    
    Parameters:
    y_data: spectral data
    lam: smoothness parameter (higher = smoother). Typical range: 1e4-1e7
    p: asymmetry parameter (0 < p < 0.5). Lower = more aggressive. Typical: 0.0001-0.01
    niter: number of iterations for weight refinement
    clip: if True, clip negative values to 0; if False, preserve for analysis
    """
    from scipy.sparse import diags
    from scipy.sparse.linalg import spsolve
    
    y = np.asarray(y_data, dtype=float)
    m = y.size
    if m < 3:
        return y.copy()
    
    # Create the 2nd-difference matrix with correct offsets
    # For shape (m-2, m), we want the operator to act on columns (i, i+1, i+2)
    D = diags([np.ones(m-2), -2*np.ones(m-2), np.ones(m-2)], [0, 1, 2], shape=(m-2, m))
    
    # Initialize weights
    w = np.ones(m)
    
    # Iteratively refine baseline and weights
    for _ in range(niter):
        W = diags(w, 0, shape=(m, m))
        # Solve: (W + lambda * D^T * D) * z = W * y
        z = spsolve(W + lam * (D.T @ D), w * y)
        z = np.asarray(z).flatten()
        
        # Update weights: p for peaks (above baseline), (1-p) for valleys (below)
        w = np.where(y > z, p, 1 - p)
    
    # Correct the data
    y_corr = y - z
    
    return np.clip(y_corr, 0, None) if clip else y_corr


# Module-level function for applying smoothing - can be imported and used elsewhere
def apply_smoothing(y_data, method, strength, *, clip_negative=True):
    """
    Apply smoothing to spectral data.

    Parameters:
    y_data: spectral data array
    method: smoothing method name (string)
    strength: smoothing strength parameter (int or float)
    clip_negative: clip Savitzky-Golay output at zero; disable when downstream
        noise statistics need symmetric residuals

    Returns:
    smoothed data array
    """
    val = strength
    
    # Apply Moving average smoothing
    if method == "Moving average":
        window_size = max(int(float(val)) // 2, 1)
        if window_size % 2 == 0:
            window_size += 1
        alpha = 1 / window_size
        y_smoothed = np.zeros_like(y_data)
        y_smoothed[0] = y_data[0]
        for i in range(1, len(y_data)):
            y_smoothed[i] = alpha * y_data[i] + (1 - alpha) * y_smoothed[i - 1]

    # Apply Gaussian filter smoothing
    elif method == "Gaussian filter":
        y_smoothed = gaussian_filter1d(y_data, sigma=val/2)

    # Apply Savitzky-Golay filter smoothing
    elif method == "Savitzky-Golay filter":
        window = max(int(val) // 2, 5)
        if window % 2 == 0:
            window += 1
        y_smoothed = savgol_filter(y_data, window_length=window, polyorder=3)
        if clip_negative:
            y_smoothed = np.clip(y_smoothed, 0, None)

    # Apply Median filter smoothing
    elif method == "Median filter":
        kernel = max(int(val) // 2, 1)
        if kernel % 2 == 0:
            kernel += 1
        y_smoothed = medfilt(y_data, kernel_size=kernel)

    # Apply Wavelet transform smoothing
    elif method == "Wavelet transform":
        coeffs = pywt.wavedec(y_data, 'coif1')
        threshold = (val/4) * np.max(np.abs(coeffs[-1]))
        coeffs_thresh = [pywt.threshold(c, value=threshold, mode="soft") for c in coeffs[1:]]
        coeffs_thresh.insert(0, coeffs[0])
        y_smoothed = pywt.waverec(coeffs_thresh, 'coif1')
        y_smoothed = np.resize(y_smoothed, len(y_data))
    
    else:
        y_smoothed = y_data

    return y_smoothed


# Module-level function for laser removal
def apply_laser_removal(y_data, x_data, center_wavelength, width):
    """
    Remove laser line from spectral data.
    
    Parameters:
    y_data: spectral data array
    x_data: wavelength array
    center_wavelength: center wavelength of laser line
    width: width of removal region around center
    
    Returns:
    data with laser line removed
    """
    result = y_data.copy()
    mask = (x_data >= center_wavelength - width) & (x_data <= center_wavelength + width)
    result[mask] = 0
    return result


# Define a function to adjust the spectrum
def adjust_spectrum(app, ax):
    if not app.line:
        messagebox.showerror("Error", "Please import data before adjusting the spectrum.", parent=app.root)
        return

    # Store the original data in case the user cancels the changes
    original_data = app.line.get_ydata().copy()

    # Define a function to restore the original data
    def restore_original_data(app, ax):
        app.line.set_ydata(original_data)
        app.y_data = original_data.copy()
        app.canvas.draw()

    x_data = app.line.get_xdata()
    y_data = app.line.get_ydata()

    global original_y_data
    original_y_data = y_data.copy()

#=======================================================================================================================
    # Create a notebook (tabbed window)
    spectrum_window = create_dialog(app.root, "Adjust Spectrum", resizable=True)
    spectrum_window.rowconfigure(0, weight=1)
    spectrum_window.columnconfigure(0, weight=1)

    # Create the main frame
    scroll_outer, main_frame = make_scrollable_frame(spectrum_window)
    scroll_outer.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

    # When the Cancel button is clicked or the window is closed, restore the original data and close the window
    def on_closing(app, ax):
        restore_original_data(app, ax)
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status("Canceled spectrum adjustment.")
        spectrum_window.destroy()

    # When the Apply button is clicked, save the current data as the original data and close the window
    def on_apply(app, ax):
        global original_y_data
        original_y_data = app.line.get_ydata().copy()
        
        # Save current settings to presets
        from prolibspector.core.settings import load_settings, save_settings
        
        # Load existing settings (to preserve plot settings if they exist)
        existing_settings = load_settings()
        if existing_settings is None:
            existing_settings = {"adjust_spectrum": {}, "adjust_plot": {}}
        
        # Update spectrum settings
        all_settings = existing_settings
        all_settings["adjust_spectrum"] = {
            "smoothing_method": smooth_method_var.get(),
            "smoothing_strength": int(float(smooth_strength_slider.get())),
            "laser_removal_enabled": laser_removal_var.get(),
            "laser_wavelength": laser_wavelength_var.get(),
            "laser_removal_width": laser_width_var.get(),
            "baseline_removal_enabled": baseline_removal_var.get(),
            "baseline_smoothness": smoothness_preset_var.get(),
            "baseline_asymmetry": asymmetry_preset_var.get()
        }
        success, msg = save_settings(all_settings)
        if not success:
            logger.warning("Could not save settings - %s", msg)
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status("Applied spectrum adjustment.")
        
        spectrum_window.destroy()

    # Create a frame for the smoothing method dropdown
    smooth_method_frame = ttk.LabelFrame(main_frame, text="Smoothing")
    smooth_method_frame.pack(fill=tk.X, expand=True, pady=10)

    # Create a label widget for the smoothing method selection
    ttk.Label(smooth_method_frame, text="Select smoothing method:").pack(anchor=tk.W, padx=(10, 0))

    # Create a dropdown menu for the smoothing method selection
    smooth_method_var = tk.StringVar()
    smooth_method_options = [
        "Moving average",
        "Gaussian filter",
        "Savitzky-Golay filter",
        "Median filter",
        "Wavelet transform"
    ]

    # Create a slider for the smoothing strength selection (packed below the
    # method dropdown it belongs to)
    smooth_strength_slider = ttk.Scale(smooth_method_frame, from_=1, to=len(x_data) // 50, orient="horizontal", length=400, command=lambda val:update_smoothed_data(val))
#=======================================================================================================================


    # Updtae the slider range when the smoothing method is changed
    def update_smooth_method(event):
        method = smooth_method_var.get()
        if method == "Moving average":
            smooth_strength_slider.configure(from_=1, to=30)  # Set maximum to 30
        elif method == "Gaussian filter":
            smooth_strength_slider.configure(from_=1, to=15)  # Set maximum to 15
        elif method == "Savitzky-Golay filter":
            smooth_strength_slider.configure(from_=1, to=100)  # Existing range
        elif method == "Median filter":
            smooth_strength_slider.configure(from_=1, to=20)  # Set maximum to 20
        elif method == "Wavelet transform":
            smooth_strength_slider.configure(from_=0, to=20)  # Set maximum to 20

    # Create a Combobox for the dropdown menu
    smooth_method_menu = ttk.Combobox(smooth_method_frame, textvariable=smooth_method_var, values=smooth_method_options, width=20, state="readonly")
    smooth_method_menu.pack(anchor=tk.W, padx=(10, 0), pady=(10, 0))
    smooth_strength_slider.pack(pady=(15, 2))

    # Readout for the current smoothing strength, directly under its slider
    slider_value_label = ttk.Label(smooth_method_frame, text="")
    slider_value_label.pack(anchor=tk.W, padx=(10, 0), pady=(0, 10))

    # Set the default value for the dropdown menu
    smooth_method_var.set(smooth_method_options[0])

    # Bind the <<ComboboxSelected>> event to the update_smooth_method function
    smooth_method_menu.bind("<<ComboboxSelected>>", update_smooth_method)
    smooth_method_menu.focus_set()


#=======================================================================================================================
    # Create laser removal frame
    laser_removal_frame = ttk.LabelFrame(main_frame, text="Laser Pointer Removal")
    laser_removal_frame.pack(fill=tk.X, expand=True, pady=10)

    # Wavelength selection frame
    wavelength_frame = ttk.Frame(laser_removal_frame)
    wavelength_frame.pack(anchor=tk.W, padx=(10, 0), pady=(10, 5))
    
    ttk.Label(wavelength_frame, text="Laser wavelength (nm):").pack(side=tk.LEFT, padx=(0, 10))
    laser_wavelength_var = tk.DoubleVar()
    laser_wavelength_var.set(532.63)  # Default wavelength
    
    laser_wavelength_spinbox = ttk.Spinbox(wavelength_frame, from_=200, to=1200, textvariable=laser_wavelength_var, width=10, command=lambda: update_spectrum_with_processing())
    laser_wavelength_spinbox.pack(side=tk.LEFT)

    # Checkbox to enable/disable laser removal
    laser_removal_var = tk.BooleanVar()
    laser_removal_checkbox = ttk.Checkbutton(laser_removal_frame, text="Remove laser pointer artifact", variable=laser_removal_var, command=lambda: update_spectrum_with_processing(), style="Switch.TCheckbutton")
    laser_removal_checkbox.pack(anchor=tk.W, padx=(10, 0), pady=(10, 5))

    # Slider for laser removal width
    ttk.Label(laser_removal_frame, text="Removal width (±nm):").pack(anchor=tk.W, padx=(10, 0))
    laser_width_var = tk.DoubleVar()
    laser_width_var.set(2.0)  # Default ±2 nm around laser wavelength
    
    # Label to show current width value
    laser_width_label = ttk.Label(laser_removal_frame, text=f"±{laser_width_var.get():.1f} nm")
    laser_width_label.pack(anchor=tk.W, padx=(10, 0), pady=(5, 0))
    
    def update_laser_width(val):
        """Update the laser width label and apply processing"""
        laser_width_label.config(text=f"±{float(val):.1f} nm")
        update_spectrum_with_processing()
    
    laser_width_slider = ttk.Scale(laser_removal_frame, from_=0.5, to=10.0, orient="horizontal", length=400, variable=laser_width_var, command=update_laser_width)
    laser_width_slider.pack(padx=(10, 10), pady=(5, 10))

#=======================================================================================================================
    # Create baseline removal frame
    baseline_removal_frame = ttk.LabelFrame(main_frame, text="Baseline Removal")
    baseline_removal_frame.pack(fill=tk.X, expand=True, pady=10)

    # Checkbox to enable/disable baseline removal
    baseline_removal_var = tk.BooleanVar()
    baseline_removal_checkbox = ttk.Checkbutton(baseline_removal_frame, text="Remove baseline", variable=baseline_removal_var, command=lambda: update_spectrum_with_processing(), style="Switch.TCheckbutton")
    baseline_removal_checkbox.pack(anchor=tk.W, padx=(10, 0), pady=(10, 5))

    # Smoothness parameter frame
    smoothness_frame = ttk.Frame(baseline_removal_frame)
    smoothness_frame.pack(anchor=tk.W, padx=(10, 0), pady=(5, 10), fill=tk.X)
    
    smoothness_label = ttk.Label(smoothness_frame, text="Smoothness (λ):")
    smoothness_label.pack(side=tk.LEFT, padx=(0, 10))
    attach_tooltip(smoothness_label, "ALS baseline stiffness: higher λ follows only broad curvature; lower λ tracks finer baseline wiggles.")
    
    smoothness_preset_var = tk.StringVar()
    smoothness_preset_var.set("Medium (1e4)")  # Default value
    
    smoothness_options = ["Low (1e3)", "Medium (1e4)", "High (1e5)"]
    smoothness_preset_menu = ttk.Combobox(smoothness_frame, textvariable=smoothness_preset_var, values=smoothness_options, width=20, state="readonly")
    smoothness_preset_menu.pack(side=tk.LEFT)
    
    # Store the actual lambda values
    smoothness_values = {
        "Low (1e3)": 1e3,
        "Medium (1e4)": 1e4,
        "High (1e5)": 1e5
    }

    # Asymmetry parameter frame
    asymmetry_frame = ttk.Frame(baseline_removal_frame)
    asymmetry_frame.pack(anchor=tk.W, padx=(10, 0), pady=(5, 10), fill=tk.X)
    
    asymmetry_label = ttk.Label(asymmetry_frame, text="Asymmetry (p):")
    asymmetry_label.pack(side=tk.LEFT, padx=(0, 10))
    attach_tooltip(asymmetry_label, "ALS peak protection: lower p treats points above the baseline as peaks more aggressively, pulling the baseline under them.")
    
    asymmetry_preset_var = tk.StringVar()
    asymmetry_preset_var.set("Balanced (0.001)")  # Default value
    
    asymmetry_options = ["Conservative (0.01)", "Balanced (0.001)", "Aggressive (0.0001)"]
    asymmetry_preset_menu = ttk.Combobox(asymmetry_frame, textvariable=asymmetry_preset_var, values=asymmetry_options, width=20, state="readonly")
    asymmetry_preset_menu.pack(side=tk.LEFT)
    
    # Store the actual p values
    asymmetry_values = {
        "Conservative (0.01)": 0.01,
        "Balanced (0.001)": 0.001,
        "Aggressive (0.0001)": 0.0001
    }
    
    # Bind the combobox events to update the spectrum
    smoothness_preset_menu.bind("<<ComboboxSelected>>", lambda event: update_spectrum_with_processing())
    asymmetry_preset_menu.bind("<<ComboboxSelected>>", lambda event: update_spectrum_with_processing())

    def update_spectrum_with_processing():
        """Update spectrum with smoothing, laser removal, and baseline removal"""
        # Start with original data
        current_data = original_y_data.copy()
        
        # Apply smoothing if needed
        if smooth_strength_slider.get() > 0:
            current_data = apply_smoothing(current_data, smooth_method_var.get(), int(float(smooth_strength_slider.get())))
        
        # Apply laser removal if enabled
        if laser_removal_var.get():
            current_data = apply_laser_removal(current_data, x_data, center_wavelength=laser_wavelength_var.get(), width=laser_width_var.get())
        
        # Apply baseline removal if enabled
        if baseline_removal_var.get():
            lam = smoothness_values[smoothness_preset_var.get()]
            p = asymmetry_values[asymmetry_preset_var.get()]
            current_data = apply_baseline_removal(current_data, lam=lam, p=p)
        
        # Update the plot
        app.y_data = current_data
        ax.relim()
        ax.autoscale_view()
        app.line.set_ydata(current_data)
        app.canvas.draw()

#=======================================================================================================================
    # Update the plot data when the smoothing method is changed
    def update_smoothed_data(val):
        if val is None:
            val = 1
        
        # Update the slider label text
        method = smooth_method_var.get()
        if method == "Moving average":
            slider_value_label.config(text=f"Window size: {int(float(val))}")
        elif method == "Gaussian filter":
            slider_value_label.config(text=f"Sigma: {val}")
        elif method == "Savitzky-Golay filter":
            slider_value_label.config(text=f"Window length: {int(float(val))}")
        elif method == "Median filter":
            slider_value_label.config(text=f"Kernel size: {int(float(val))}")
        elif method == "Wavelet transform":
            slider_value_label.config(text=f"Threshold: {val}")
        
        # Use the combined processing function
        update_spectrum_with_processing()

    # Call the update_smoothed_data function to apply the initial smoothing
    update_smoothed_data(None)

#=======================================================================================================================

    # Create a frame to hold the Cancel, Apply and Help button
    button_frame = ttk.Frame(spectrum_window)
    button_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))

    # Add the Cancel and Apply buttons
    cancel_button = ttk.Button(button_frame, text="Cancel", command=lambda: on_closing(app, ax))
    cancel_button.pack(side="left", padx=10)

    apply_button = ttk.Button(button_frame, text="Apply", command=lambda: on_apply(app, ax), style="Accent.TButton")
    apply_button.pack(side="left", padx=10)

    # Add the Help button
    help_button = ttk.Button(button_frame, text="Help", command=lambda: open_help_document())
    help_button.pack(side="left", padx=10)

    # Override the default behavior when closing the window
    spectrum_window.protocol("WM_DELETE_WINDOW", lambda: on_closing(app, ax))
    spectrum_window.bind("<Return>", lambda _event: on_apply(app, ax))
    apply_window_policy(
        spectrum_window,
        parent=app.root,
        preferred=(640, 760),
        min_size=(520, 460),
        modal=False,
        resizable=True,
        remember_key="adjust_spectrum",
    )

#=======================================================================================================================


def open_help_document():
    # Define your markdown text
    markdown_text = """
# Help Section: Smoothing LIBS Data and Laser Removal

## Overview

This section provides two main preprocessing functions for LIBS data: smoothing to reduce noise and laser pointer artifact removal. These tools help clean your spectral data before analysis.

## Smoothing Methods

### Why Smooth LIBS Data?

Smoothing data in Laser-Induced Breakdown Spectroscopy (LIBS) is a common preprocessing step that helps to reduce noise and enhance signal-to-noise ratio, allowing for better interpretation and analysis of the spectral data.

This section allows you to select from several smoothing methods, each with its own strengths and best use cases. Below, you'll find a brief description of each method.

### Moving Average

The moving average method is a straightforward smoothing technique that works by creating a new series where the values are computed as the average of raw data points in a sliding window across the data set. This window moves along the data, calculating the average of the points within the window, and assigns this average to the central point. This method is simple, intuitive and effective, particularly for removing random, high-frequency noise.

### Gaussian Filter

The Gaussian filter is a more sophisticated smoothing technique that convolves the data with a Gaussian function. This function is bell-shaped and has nice properties, such as having the same shape in the time and frequency domains, which makes it useful for a variety of applications. The Gaussian filter can handle a wider variety of noise patterns than the moving average method, making it a better choice when the noise in your data is not uniformly distributed or when it's correlated.

### Savitzky-Golay Filter

The Savitzky-Golay filter, also known as digital smoothing polynomial filter or least squares smoothing filter, applies a polynomial fit to a window of data points and replaces each point with the value from the polynomial. This method is particularly good for preserving spectral features such as peak height and width, which can be important in LIBS data.

### Median Filter

The Median filter is a type of nonlinear filter that replaces each data point with the median of neighboring points. The main advantage of this filter over linear filters like the moving average or the Gaussian filter is its ability to remove 'salt and pepper' noise effectively. This method can be incredibly useful for LIBS data with sharp, sudden disturbances or outliers.

### Wavelet Transform

The Wavelet transform method, a relatively modern technique, uses wavelets to both decompose and reconstruct the signal. Wavelets, unlike other techniques that use a fixed basis, allow for multi-resolution analysis, meaning that they can analyze the signal at different frequencies with different resolutions.

## Laser Removal (532.63 nm)

### Why Remove Laser Artifacts?

Many LIBS instruments use a red pointer laser (532.63 nm) to help with focusing and targeting. If this laser is not properly turned off during measurement, it can create an artificial peak in your spectrum at 532.63 nm. This contamination can interfere with analysis, especially if there are legitimate spectral lines nearby.

### How It Works

The laser removal function identifies the wavelength region around 532.63 nm and sets the intensity values in that region to zero. This effectively removes the laser artifact without affecting the rest of your spectrum.

### Controls

- **Enable/Disable**: Use the checkbox to turn laser removal on or off
- **Removal Width**: The slider controls how wide an area around 532.63 nm gets zeroed out
  - **Narrow (±0.5-1 nm)**: For minimal laser interference
  - **Medium (±2-3 nm)**: Good default for most cases
  - **Wide (±5-10 nm)**: For strong laser contamination with broad base

### Usage Tips

1. **Check your spectrum first**: Look for an unexpected peak at 532.63 nm
2. **Start narrow**: Begin with ±1-2 nm and increase if needed
3. **Avoid over-removal**: Don't use wider ranges than necessary to preserve nearby real peaks
4. **Combine with smoothing**: You can use both laser removal and smoothing together
5. **Visual feedback**: Watch the plot update in real-time as you adjust the width

### When to Use

- When you see an artificial peak at exactly 532.63 nm
- If the laser pointer was accidentally left on during measurement
- When you need to clean the spectrum before automated peak detection
- For ensuring accurate quantitative analysis in the green spectral region

*Remember: Only use laser removal if you're certain there's laser contamination. Legitimate spectral lines near 532.63 nm should not be removed.*
    """

# Convert the markdown to HTML
    html_text = markdown.markdown(markdown_text)

    make_markdown_help_window(
        tk._default_root,
        "Adjust Spectrum Help",
        lambda: html_text,
        lambda parent, html: HTMLLabel(parent, html=html),
    )





