# adjust_spectrum.py - Contains the adjust_spectrum function and all the necessary helper functions for spectrum adjustment.

import tkinter
import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
from tkinter import Toplevel
import numpy as np
import scipy
import pywt
from scipy.signal import savgol_filter, medfilt
from scipy.ndimage import gaussian_filter1d
from tkinter import messagebox
import markdown
from tkhtmlview import HTMLLabel


# Define a function to adjust the spectrum
def adjust_spectrum(app, ax):
    if not app.line:
        messagebox.showerror("Error", "Please import data before adjusting the spectrum.")
        return

    # Store the original data in case the user cancels the changes
    original_data = app.line.get_ydata().copy()

    # Define a function to restore the original data
    def restore_original_data(app, ax):
        app.line.set_ydata(original_data)
        app.canvas.draw()

    x_data = app.line.get_xdata()
    y_data = app.line.get_ydata()

    global original_y_data
    original_y_data = y_data.copy()

#=======================================================================================================================
    # Create a notebook (tabbed window)
    spectrum_window = tkinter.Toplevel()
    spectrum_window.title("Adjust Spectrum")
    spectrum_window.geometry("600x280")  # Set window size to 400x200 pixels

    # Create the main frame 
    main_frame = ttk.Frame(spectrum_window)  
    main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # Create a label for the current smoothing strength
    slider_value_label = ttk.Label(spectrum_window, text="")
    slider_value_label.pack(pady=5)
    # Move the slider_value_label to the bottom of the window
    slider_value_label.pack(side="bottom", pady=10)

    # When the Cancel button is clicked or the window is closed, restore the original data and close the window
    def on_closing(app, ax):
        restore_original_data(app, ax)
        spectrum_window.destroy()

    # When the Apply button is clicked, save the current data as the original data and close the window
    def on_apply(app, ax):
        global original_y_data
        original_y_data = app.line.get_ydata().copy()
        spectrum_window.destroy()

    # Create a frame for the smoothing method dropdown 
    smooth_method_frame = ttk.LabelFrame(main_frame)  
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

    # Create a slider for the smoothing strength selection
    smooth_strength_slider = ttk.Scale(smooth_method_frame, from_=1, to=len(x_data) // 50, orient="horizontal", length=400, command=lambda val:update_smoothed_data(val))
    smooth_strength_slider.pack(pady=15)
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
    smooth_method_menu = ttk.Combobox(smooth_method_frame, textvariable=smooth_method_var, values=smooth_method_options, width=20)
    smooth_method_menu.pack(anchor=tk.W, padx=(10, 0), pady=(10, 0))

    # Set the default value for the dropdown menu
    smooth_method_var.set(smooth_method_options[0])

    # Bind the <<ComboboxSelected>> event to the update_smooth_method function
    smooth_method_menu.bind("<<ComboboxSelected>>", update_smooth_method)


#=======================================================================================================================
    # Create laser removal frame
    laser_removal_frame = ttk.LabelFrame(main_frame, text="Laser Removal (532.63 nm)")
    laser_removal_frame.pack(fill=tk.X, expand=True, pady=10)

    # Checkbox to enable/disable laser removal
    laser_removal_var = tk.BooleanVar()
    laser_removal_checkbox = ttk.Checkbutton(laser_removal_frame, text="Remove laser pointer artifact", variable=laser_removal_var, command=lambda: update_spectrum_with_processing())
    laser_removal_checkbox.pack(anchor=tk.W, padx=(10, 0), pady=(10, 5))

    # Slider for laser removal width
    ttk.Label(laser_removal_frame, text="Removal width (±nm):").pack(anchor=tk.W, padx=(10, 0))
    laser_width_var = tk.DoubleVar()
    laser_width_var.set(2.0)  # Default ±2 nm around 532.63
    
    # Label to show current width value
    laser_width_label = ttk.Label(laser_removal_frame, text=f"±{laser_width_var.get():.1f} nm")
    laser_width_label.pack(anchor=tk.W, padx=(10, 0), pady=(5, 0))
    
    def update_laser_width(val):
        """Update the laser width label and apply processing"""
        laser_width_label.config(text=f"±{float(val):.1f} nm")
        update_spectrum_with_processing()
    
    laser_width_slider = ttk.Scale(laser_removal_frame, from_=0.5, to=10.0, orient="horizontal", length=400, variable=laser_width_var, command=update_laser_width)
    laser_width_slider.pack(padx=(10, 10), pady=(5, 10))

    def apply_laser_removal(y_data, x_data, center_wavelength=532.63, width=2.0):
        """Remove laser pointer artifact by zeroing out the specified wavelength range"""
        y_processed = y_data.copy()
        
        # Find indices within the removal range
        mask = (x_data >= center_wavelength - width) & (x_data <= center_wavelength + width)
        
        # Set those values to zero
        y_processed[mask] = 0
        
        return y_processed

    def apply_smoothing(val, smooth_method_var):
        # Apply Moving average smoothing
        if smooth_method_var.get() == "Moving average":
            window_size = max(int(float(val)) // 2, 1)  # Divide window size by 2
            if window_size % 2 == 0:
                window_size += 1
            alpha = 1 / (window_size)
            y_smoothed = np.zeros_like(y_data)
            y_smoothed[0] = y_data[0]
            for i in range(1, len(y_data)):
                y_smoothed[i] = alpha * y_data[i] + (1 - alpha) * y_smoothed[i - 1]

        # Apply Gaussian filter smoothing
        elif smooth_method_var.get() == "Gaussian filter":
            y_smoothed = scipy.ndimage.gaussian_filter(y_data, (val/2))  # Divide sigma by 2

        # Apply Savitzky-Golay filter smoothing
        elif smooth_method_var.get() == "Savitzky-Golay filter":
            val = max(val // 2, 5)  # Divide window length by 2
            if val % 2 == 0:
                val += 1
            y_smoothed = scipy.signal.savgol_filter(y_data, window_length=val, polyorder=3)
            y_smoothed = np.clip(y_smoothed, 0, None)

        # Apply Median filter smoothing
        elif smooth_method_var.get() == "Median filter":
            val = max(int(val) // 2, 1)  # Divide kernel size by 2
            if val % 2 == 0:
                val += 1
            y_smoothed = scipy.signal.medfilt(y_data, kernel_size=val)

        # Apply Wavelet transform smoothing
        elif smooth_method_var.get() == "Wavelet transform":
            coeffs = pywt.wavedec(y_data, 'coif1')
            threshold = (val/4) * np.max(np.abs(coeffs[-1]))  # Divide threshold multiplier by 4
            coeffs_thresh = [pywt.threshold(c, value=threshold, mode="soft") for c in coeffs[1:]]
            coeffs_thresh.insert(0, coeffs[0])
            y_smoothed = pywt.waverec(coeffs_thresh, 'coif1')
            y_smoothed = np.resize(y_smoothed, len(y_data))  # Resize y_smoothed to have the same length as y_data

        # If no smoothing method is selected, return the original data
        else:
            y_smoothed = y_data

        return y_smoothed

    def update_spectrum_with_processing():
        """Update spectrum with both smoothing and laser removal"""
        # Start with original data
        current_data = original_y_data.copy()
        
        # Apply smoothing if needed
        if smooth_strength_slider.get() > 0:
            current_data = apply_smoothing(int(float(smooth_strength_slider.get())), smooth_method_var)
        
        # Apply laser removal if enabled
        if laser_removal_var.get():
            current_data = apply_laser_removal(current_data, x_data, width=laser_width_var.get())
        
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
    button_frame.pack(side="bottom", pady=10)

    # Add the Cancel and Apply buttons
    cancel_button = ttk.Button(button_frame, text="Cancel", command=lambda: on_closing(app, ax))
    cancel_button.pack(side="left", padx=10)

    apply_button = ttk.Button(button_frame, text="Apply", command=lambda: on_apply(app, ax))
    apply_button.pack(side="left", padx=10)

    # Add the Help button
    help_button = ttk.Button(button_frame, text="Help", command=lambda: open_help_document())
    help_button.pack(side="left", padx=10)

    # Override the default behavior when closing the window
    spectrum_window.protocol("WM_DELETE_WINDOW", lambda: on_closing(app, ax))

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

    # Create a new tkinter window
    help_window = tk.Toplevel()
    help_window.title("Help")

    # Set the window to open in fullscreen
    help_window.attributes('-fullscreen', True)

    help_window.bind("<Escape>", lambda event: help_window.attributes("-fullscreen", False))

    # Create an HTMLLabel to display the HTML
    html_label = HTMLLabel(help_window, html=html_text)
    html_label.pack(fill="both", expand=True)





