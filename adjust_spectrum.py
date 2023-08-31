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
    # Update the plot data when the smoothing method is changed
    def update_smoothed_data(val):
        if val is None:
            val = 1
        y_smoothed = apply_smoothing(int(float(val)), smooth_method_var)
        app.y_data = y_smoothed  # Update app.y_data with the smoothed data
        ax.relim()
        ax.autoscale_view()  # Use autoscaling when not normalizing
        app.line.set_ydata(y_smoothed)
        app.canvas.draw()

        # Update the label text based on the smoothing method
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
    # Apply Wavelet transform smoothing
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
# Help Section: Smoothing LIBS Data

## Why Smooth LIBS Data?

Smoothing data in Laser-Induced Breakdown Spectroscopy (LIBS) is a common preprocessing step that helps to reduce noise and enhance signal-to-noise ratio, allowing for better interpretation and analysis of the spectral data.

This tab allows you to select from several smoothing methods, each with its own strengths and best use cases. Below, you'll find a brief description of each method.

## Smoothing Methods

### Moving Average

The moving average method is a straightforward smoothing technique that works by creating a new series where the values are computed as the average of raw data points in a sliding window across the data set. This window moves along the data, calculating the average of the points within the window, and assigns this average to the central point. This method is simple, intuitive and effective, particularly for removing random, high-frequency noise. It's a linear filter that helps to clarify the patterns in the data by reducing variation. However, one limitation is that it may not be as effective for more complex or non-uniform noise patterns. It also tends to reduce peak values and can distort the signal when the window size is not appropriately chosen. Despite these potential downsides, the moving average method is a great starting point for smoothing LIBS data due to its simplicity and effectiveness in many situations.

### Gaussian Filter

The Gaussian filter is a more sophisticated smoothing technique that convolves the data with a Gaussian function. This function is bell-shaped and has nice properties, such as having the same shape in the time and frequency domains, which makes it useful for a variety of applications. The Gaussian filter can handle a wider variety of noise patterns than the moving average method, making it a better choice when the noise in your data is not uniformly distributed or when it's correlated. One of the key strengths of the Gaussian filter is that it retains the overall shape of the data better than many other smoothing methods, preserving peak heights and not introducing artificial oscillations. This attribute is crucial if your LIBS data has complex noise patterns and preserving peak shapes is important. One thing to consider is that the width of the Gaussian function (the standard deviation) controls the level of smoothing: a large width will result in more smoothing, while a small width will retain more details.

### Savitzky-Golay Filter

The Savitzky-Golay filter, also known as digital smoothing polynomial filter or least squares smoothing filter, applies a polynomial fit to a window of data points and replaces each point with the value from the polynomial. This is a type of convolution where the coefficients are not constant but depend on the data. This method is particularly good for preserving spectral features such as peak height and width, which can be important in LIBS data. The filter has the advantage of preserving higher moments, and it performs well when the underlying data is a polynomial function of the independent variable. This makes the Savitzky-Golay filter especially useful for LIBS data with well-defined peaks that you wish to preserve. While this method can introduce artifacts if the degree of the polynomial is too high, choosing the correct window size and polynomial degree can yield excellent results.

### Median Filter

The Median filter is a type of nonlinear filter that replaces each data point with the median of neighboring points. The main advantage of this filter over linear filters like the moving average or the Gaussian filter is its ability to remove 'salt and pepper' noise effectively. This kind of noise is characterized by sharp and sudden disturbances in the image, and it's called 'salt and pepper' because it presents itself as sparsely occurring white and black pixels. The median filter is particularly effective at preserving edges, which are abrupt changes in the signal. This method can be incredibly useful for LIBS data with sharp, sudden disturbances or outliers, as it can smooth the data without reducing the sharpness of the signal, which is crucial for identifying spectral lines.

### Wavelet Transform

The Wavelet transform method, a relatively modern technique, uses wavelets to both decompose and reconstruct the signal. Wavelets, unlike other techniques that use a fixed basis, allow for multi-resolution analysis, meaning that they can analyze the signal at different frequencies with different resolutions. This method is very flexible and can handle a wide variety of noise patterns, making it a robust choice for complex datasets. Unlike many traditional methods, wavelet transform allows for both time and frequency analysis, which can be extremely beneficial for data where non-stationary or transient characteristics are present.

Wavelets are particularly well-suited for data where there are abrupt changes or discontinuities, or where the signal frequency changes over time. In the context of LIBS data, this can be advantageous when the spectral data contains rapidly changing features, or when the noise varies across different spectral regions.

However, the wavelet method is also more complex compared to the other methods mentioned. It involves choices about the method for thresholding the wavelet coefficients. The settings can have a significant impact on the results, and the best choice often depends on the specifics of the data and the analysis task. Despite this complexity, the wavelet transform is a powerful tool and is worth considering when other simpler methods do not provide satisfactory results. Moreover, the ability to perform multi-resolution analysis can provide insights and data features that may not be captured by other smoothing methods.

## Slider Strength

### Moving Average - Window Size

The slider for the Moving Average method adjusts the "window size". This is the number of data points included in each calculation of the average. A larger window size will result in more smoothing because each output point will be the average of more input points. Conversely, a smaller window size will result in less smoothing. You should choose a window size that best reduces noise while preserving important features of the signal.

### Gaussian Filter - Sigma

For the Gaussian Filter, the slider adjusts the "sigma" value. Sigma is the standard deviation of the Gaussian function used in the smoothing process. A larger sigma value will result in more smoothing because the Gaussian function will be wider, spreading the influence of each data point over a larger area. Conversely, a smaller sigma value will result in less smoothing. The choice of sigma should balance noise reduction and preservation of important signal features.

### Savitzky-Golay Filter - Window Length

The slider for the Savitzky-Golay Filter adjusts the "window length". This is the number of data points used to fit the polynomial in each step of the filter. A larger window length will result in more smoothing because each output point is influenced by more input points. Conversely, a smaller window length will result in less smoothing. The window length should be chosen to best balance noise reduction and the preservation of important signal features.

### Median Filter - Kernel Size

For the Median Filter, the slider adjusts the "kernel size". This is the number of data points considered when determining the median value to replace each point. A larger kernel size will result in more smoothing because each output point is the median of more input points. Conversely, a smaller kernel size will result in less smoothing. The kernel size should be chosen to best eliminate noise while maintaining important signal features.

### Wavelet Transform - Threshold

In the case of the Wavelet Transform, the slider adjusts the "threshold". This value is used to shrink the wavelet coefficients in the wavelet transform. Coefficients with an absolute value smaller than the threshold are set to zero, reducing the complexity of the signal and thus the amount of noise. A higher threshold value will result in more smoothing, while a lower threshold will result in less smoothing. The threshold should be set so that it removes as much noise as possible without eliminating important signal features.

### *Notes*

\*Remember, each LIBS dataset can be unique, and there may be no one-size-fits-all smoothing method. Feel free to experiment with these different methods and choose the one that best fits your data.
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





