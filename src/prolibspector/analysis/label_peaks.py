"""Peak-labeling helpers for annotated analysis plots."""

import tkinter as tk
from tkinter import ttk
from scipy.signal import argrelextrema
import numpy as np
from tkinter import messagebox
import pandas as pd
import matplotlib.lines as mlines
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('TkAgg')
import numpy as np
import time
import re
import textalloc as ta
import markdown
from tkhtmlview import HTMLLabel

from prolibspector.core.tooltip import attach_tooltip
from prolibspector.core.ui_theme import get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog, make_markdown_help_window


def label_peaks(app, ax, element_df):
    """Label detected peaks and open the matching controls dialog."""

    def tolerance_window(app, ax, element_df):
        tolerance_window = create_dialog(app.root, "Label Peaks", resizable=True)

        # Create the font-size slider.
        ttk.Label(tolerance_window, text="Font size:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        font_size_var = tk.IntVar()
        font_size_var.set(8)
        font_size_slider = ttk.Scale(tolerance_window, from_=5, to=20, orient=tk.HORIZONTAL, variable=font_size_var, length=300)
        font_size_slider.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Function to update slider label
        def update_font_size_slider_label(app, ax, font_size_var):
            font_size_slider_label.config(text=f"{font_size_var.get()}")
            update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var)

        font_size_var.trace("w", lambda *args: update_font_size_slider_label(app, ax, font_size_var))

        # Create the font size slider label
        font_size_slider_label = ttk.Label(tolerance_window, text=f"{font_size_var.get()}")
        font_size_slider_label.grid(row=0, column=2, padx=15, pady=15, sticky="w")


         # Create the intensity threshold slider ##########################################
        ttk.Label(tolerance_window, text="Intensity Threshold (%):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        intensity_var = tk.IntVar()
        intensity_var.set(10)
        intensity_slider = ttk.Scale(tolerance_window, from_=1, to=100, orient=tk.HORIZONTAL, variable=intensity_var, length=300)
        intensity_slider.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        attach_tooltip(
            intensity_slider,
            "Only peaks taller than this percentage of the strongest peak are labeled.",
        )

        # Function to update slider 
        def update_slider_label(*args):
          slider_label.config(text=f"{intensity_var.get()}%")
          update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var)

        intensity_var.trace("w", update_slider_label)

        # Create the intensity threshold slider label
        slider_label = ttk.Label(tolerance_window, text=f"{intensity_var.get()}%")
        slider_label.grid(row=1, column=2, columnspan=2, padx=15, pady=15, sticky="w")

        # Create the prominence filter slider ##########################################
        ttk.Label(tolerance_window, text="Prominence Filter (%):").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        prominence_var = tk.IntVar()
        prominence_var.set(15)  # 15% prominence by default
        prominence_slider = ttk.Scale(tolerance_window, from_=0, to=200, orient=tk.HORIZONTAL, variable=prominence_var, length=300)
        prominence_slider.grid(row=2, column=1, padx=5, pady=5, sticky="w")
        attach_tooltip(
            prominence_slider,
            "How much a peak must rise above its local surroundings to count. "
            "Raise this to drop shoulders and noise bumps.",
        )

        # Function to update prominence slider label
        def update_prominence_slider_label(app, ax, prominence_var):
            prominence_slider_label.config(text=f"{prominence_var.get()}%")
            update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var)

        prominence_var.trace("w", lambda *args: update_prominence_slider_label(app, ax, prominence_var))

        # Create the prominence slider label
        prominence_slider_label = ttk.Label(tolerance_window, text=f"{prominence_var.get()}%")
        prominence_slider_label.grid(row=2, column=2, padx=15, pady=15, sticky="w")

        # Create the Tolerance (nm) slider ##########################################
        ttk.Label(tolerance_window, text="Tolerance (nm):").grid(row=3, column=0, padx=5, pady=5, sticky="e")
        # Use DoubleVar for floating point values
        tolerance_var = tk.DoubleVar() 
        tolerance_var.set(0.3) # Set a sensible default
        
        tolerance_slider = ttk.Scale(
            tolerance_window, 
            from_=0.05, 
            to=1.5, 
            orient=tk.HORIZONTAL, 
            variable=tolerance_var, 
            length=300
        )
        # You need to manually set the resolution on the variable's trace, not the widget
        tolerance_slider.grid(row=3, column=1, padx=5, pady=5, sticky="w")
        attach_tooltip(
            tolerance_slider,
            "Maximum distance between a detected peak and a database line for "
            "the peak to be tagged with that element.",
        )
        
        # Create the label that will display the slider's value
        tolerance_slider_label = ttk.Label(tolerance_window, text=f"{tolerance_var.get():.2f}")
        tolerance_slider_label.grid(row=3, column=2, padx=15, pady=15, sticky="w")
        
        # Function to update the label and the plot
        def update_tolerance_label(*args):
            # Round the value for display
            current_val = tolerance_var.get()
            rounded_val = round(current_val / 0.05) * 0.05 # Snap to 0.05 increments
            tolerance_slider_label.config(text=f"{rounded_val:.2f}")
            # Note: The variable passed to update_labels is now tolerance_var
            update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var)

        # Link the slider's movement to the update function
        tolerance_var.trace("w", update_tolerance_label)


        ################ Buttons ################
        buttons_frame = ttk.LabelFrame(tolerance_window)
        buttons_frame.grid(row=5, column=0, columnspan=7, padx=5, pady=5)

        # Set the same weight for each column in the buttons_frame
        for col in range(6):
            buttons_frame.grid_columnconfigure(col, weight=1)

        # Create the Hide unlabeled peaks toggle button
        hide_unlabeled_peaks_var = tk.BooleanVar()
        hide_unlabeled_peaks_button = ttk.Checkbutton(buttons_frame, text="Hide unlabeled peaks", variable=hide_unlabeled_peaks_var, command=lambda: toggle_unlabeled_peaks(app, ax, hide_unlabeled_peaks_var), style="Toggle.TButton")
        hide_unlabeled_peaks_button.grid(row=1, column=0, columnspan=6, sticky='ew', padx=5, pady=5)

        # Create the Back button (return to periodic table)
        def go_back():
            delete_labels(app, ax)
            tolerance_window.destroy()
            if hasattr(app, 'periodic_window') and app.periodic_window.winfo_exists():
                app.periodic_window.deiconify()

        back_button = ttk.Button(buttons_frame, text="\u2190 Back", command=go_back)
        back_button.grid(row=0, column=0, sticky='ew', padx=5, pady=5)

        # Create the Delete labels button
        erase_button = ttk.Button(buttons_frame, text="Delete Labels", command=lambda: delete_labels(app, ax))
        erase_button.grid(row=0, column=1, sticky='ew', padx=5, pady=5)

        # Create the Regenerate labels button (restores labels after deletion using current slider settings)
        regenerate_button = ttk.Button(buttons_frame, text="Regenerate Labels", command=lambda: update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var))
        regenerate_button.grid(row=0, column=2, sticky='ew', padx=5, pady=5)

        # Create the Reduce overlap button
        apply_button = ttk.Button(buttons_frame, text="Reduce label overlap", command=lambda: adjust_labels(app, ax, intensity_var, font_size_var, time_lim=3))
        apply_button.grid(row=0, column=3, sticky='ew', padx=5, pady=5)

        # Add the Help button
        help_button = ttk.Button(buttons_frame, text="Help", command=open_help_document)
        help_button.grid(row=0, column=4, sticky='ew', padx=5, pady=5)

        # Create the Close button
        close_button = ttk.Button(buttons_frame, text="Close", command=tolerance_window.destroy)
        close_button.grid(row=0, column=5, sticky='ew', padx=5, pady=5)

        update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var)
        apply_window_policy(
            tolerance_window,
            parent=app.root,
            preferred=(760, 300),
            min_size=(560, 260),
            modal=False,
            resizable=True,
        )

    # call the tolerance_window function
    tolerance_window(app, ax, element_df)

# ================================================================================================
# Update labels function
def update_labels(app, ax, intensity_var, tolerance_var, font_size_var, prominence_var, hide_unlabeled_peaks_var):  # Add round_off_error_tolerance_var to the function arguments
    # Update the intensity threshold
    app.current_threshold_percent = intensity_var.get()
    save_peak_values(app)

    # Remove existing lines from the axes matplotlib
    for line in ax.lines[1:]:
        line.remove()
    x_data = app.x_data
    y_data = app.y_data if isinstance(app.y_data, np.ndarray) else app.y_data.to_numpy()


    if x_data.size == 0 or y_data.size == 0:
        return
    # Remove the legend   
    ax.lines[0].set_label(None)
    ax.legend_ = None
    # Remove existing peak labels
    for text_obj in app.peak_labels:
        text_obj.remove()
    app.peak_labels = []
    app.peak_values = []

    # Calculate the threshold based on the maximum value in the plot
    threshold = app.current_threshold_percent / 100.0 * y_data.max()  # Replace threshold_percent with app.current_threshold_percent

    # Find the indices of the peaks
    peak_indices = argrelextrema(y_data, comparator=np.greater_equal, order=3)
    element_df = app.element_df  # Access element_df from app

    # Apply prominence filter if enabled (value > 0)
    if prominence_var.get() > 0:
        prominence_threshold = prominence_var.get() / 100.0  # Convert percentage to ratio
        window_size = 20  # Fixed window size for simplicity
        
        filtered_peaks = []
        for idx in peak_indices[0]:
            if y_data[idx] >= threshold:  # First apply absolute threshold
                # Calculate local baseline for prominence
                start = max(0, idx - window_size)
                end = min(len(y_data), idx + window_size)
                
                # Get local region excluding the peak area
                local_region = np.concatenate([
                    y_data[start:max(start, idx-5)],
                    y_data[min(idx+5, end):end]
                ])
                
                if len(local_region) > 0:
                    local_baseline = np.mean(local_region)
                    prominence = (y_data[idx] - local_baseline) / local_baseline
                    
                    if prominence >= prominence_threshold:
                        filtered_peaks.append(idx)
                else:
                    filtered_peaks.append(idx)  # Keep peak if no local region available
        
        final_peak_indices = filtered_peaks
    else:
        # Use original filtering (absolute threshold only)
        final_peak_indices = [idx for idx in peak_indices[0] if y_data[idx] >= threshold]

     # Label the peaks with element symbols and ionization levels if possible
    texts = []  # store text objects for adjust_text
    for idx in final_peak_indices:
            label = f"{x_data[idx]:.2f} nm" 
            
            # Check if element_df is not None before accessing it
            if element_df is not None:
                tol = tolerance_var.get() #        # e.g. slider 0 → 1 nm, 1 → 0.1 nm
                element_df["Wavelength"] = pd.to_numeric(element_df["Wavelength"], errors="coerce")
                wavelengths = element_df["Wavelength"].dropna()
                
                diff = np.abs(wavelengths - x_data[idx])
                if (diff <= tol).any():                                # at least one library line within tolerance
                    match_idx = diff.idxmin()                          # pick the closest one
                    element_symbol = element_df.at[match_idx, "Symbol"]
                    ionization_level = element_df.at[match_idx, "Ionization Level"]
                    label += f" ({element_symbol}, {ionization_level})"


            # Add the following two lines to create the text object and append it to the texts list
            text_obj = ax.text(
                x_data[idx],
                y_data[idx],
                label,
                fontsize=font_size_var.get(),
                ha='center',
                va='bottom',
                color=get_palette()["text"],
            )
            if hide_unlabeled_peaks_var.get() and label.endswith("nm"):
                text_obj.set_visible(False)
            texts.append(text_obj)


    # Update app.peak_labels with the adjusted text objects
    app.peak_labels = texts

    # Save peak values
    save_peak_values(app)

    # Extract peak info after labeling the peaks
    extract_peak_info(app)
    if hasattr(app, "set_analysis_status"):
        app.set_analysis_status(
            f"Displayed {len(app.peak_labels)} peak label(s); {len(app.peak_data)} matched line(s)."
        )

    # Redraw the canvas
    app.canvas.draw()


    # ================================================================================================
    ######### Buttons functions #########

# Hide unlabeled peaks button function ########
def toggle_unlabeled_peaks(app, ax, hide_unlabeled_peaks_var):
    for text_obj in app.peak_labels:
        if text_obj.get_text().endswith("nm"):
            text_obj.set_visible(not hide_unlabeled_peaks_var.get())
    app.canvas.draw()

def delete_labels(app, ax):
    # Remove connector lines and vertical indicators (keep spectrum line at index 0)
    while len(ax.lines) > 1:
        ax.lines[-1].remove()

    # Remove all text objects from the axes
    for text_obj in list(ax.texts):
        text_obj.remove()

    app.peak_labels = []
    app.peak_values = []
    app.peak_data = []
    if hasattr(app, "set_analysis_status"):
        app.set_analysis_status("Deleted peak labels.")

    # Redraw the canvas
    app.canvas.draw()

#================================================================================================
def adjust_labels(app, ax, intensity_var, font_size_var, time_lim=3):
    if not app.peak_labels or not app.peak_values:
        return

    threshold = app.current_threshold_percent / 100.0 * max(app.peak_values)

    # Filter labels based on visibility and threshold, keep original intensities
    filtered_labels = [(text_obj, peak_value) for text_obj, peak_value in
                       zip(app.peak_labels, app.peak_values)
                       if text_obj.get_visible() and peak_value >= threshold]

    if not filtered_labels:
        return

    x = [text_obj.get_position()[0] for text_obj, _ in filtered_labels]
    y = [peak_value for _, peak_value in filtered_labels]  # Use actual peak intensities as anchor points
    text_list = [text_obj.get_text() for text_obj, _ in filtered_labels]
    original_intensities = list(y)  # Preserve for peak_values

    # Remove old labels and connector lines before textalloc creates new ones
    for text_obj in list(ax.texts):
        text_obj.remove()
    while len(ax.lines) > 1:
        ax.lines[-1].remove()

    # Get spectrum line data so textalloc avoids placing labels on top of it
    x_line = np.array(app.x_data) if not isinstance(app.x_data, np.ndarray) else app.x_data
    y_line = np.array(app.y_data) if not isinstance(app.y_data, np.ndarray) else app.y_data

    # Adjust the labels:
    #   direction="north" - prefer placing labels above data points
    #   x_lines/y_lines  - avoid overlapping with the spectrum line
    #   margin            - add spacing between labels and objects
    #   avoid_label_lines_overlap - prevent connector lines from overlapping
    palette = get_palette()
    try:
        app.root.config(cursor="watch")
        app.root.update_idletasks()
    except tk.TclError:
        pass
    try:
        result = ta.allocate(
            ax, x, y, text_list,
            x_scatter=x, y_scatter=y,
            x_lines=[x_line], y_lines=[y_line],
            textsize=font_size_var.get(),
            linecolor=palette["muted_text"],
            direction="north",
            margin=0.01,
            avoid_label_lines_overlap=True
        )
    finally:
        try:
            app.root.config(cursor="")
        except tk.TclError:
            pass

    # Capture textalloc's text objects (3rd element of returned tuple)
    _, _, text_objects, _ = result
    for text_obj in text_objects:
        text_obj.set_color(palette["text"])
    app.peak_labels = list(text_objects)
    app.peak_values = original_intensities  # Keep actual intensities, not adjusted label positions

    # Update peak data for export
    extract_peak_info(app)
    if hasattr(app, "set_analysis_status"):
        app.set_analysis_status(f"Reduced overlap for {len(app.peak_labels)} visible peak label(s).")

    # Redraw the canvas
    app.canvas.draw()

#================================================================================================
# The peak values are now stored in app.peak_values, and you can access them in other functions.
def save_peak_values(app):
    app.peak_values = [text_obj.get_position()[1] for text_obj in app.peak_labels]

# Create the update label_peaks function: ########
def update_peak_labels(app, ax, intensity_var, round_off_error_tolerance_var):
    app.current_threshold_percent = intensity_var.get()
    app.round_off_error_tolerance = round_off_error_tolerance_var.get()  # Update round-off error tolerance from the argument
    save_peak_values(app)
    

# Create the erase overlapping function: ########
def erase_labels(app, ax):
    # Remove existing peak labels
    for text_obj in app.peak_labels:
        text_obj.remove()

    app.peak_labels = []
    app.peak_values = []


    # Redraw the canvas
    app.canvas.draw() 

#================================================================================================

def extract_peak_info(app):
    peak_data = []
    for i, text_obj in enumerate(app.peak_labels):
        # Split the label text to separate the wavelength from the rest
        full_label = text_obj.get_text()
        label_parts = full_label.split(' (')
        if len(label_parts) == 2:
            wavelength, rest = label_parts
            wavelength = float(wavelength.rstrip('nm'))
            # Check if rest contains a comma
            if ',' in rest:
                # Process the remaining string to extract element symbol and ionization level
                rest = rest[:-1]  # Remove the closing parenthesis
                element_symbol, ionization_level = rest.split(',')
                # Use stored peak intensity (survives textalloc repositioning)
                relative_intensity = app.peak_values[i] if i < len(app.peak_values) else text_obj.get_position()[1]
                peak_data.append([wavelength, element_symbol.strip(), ionization_level.strip(), relative_intensity])
            else:
                pass  # Skip the label if it does not contain a comma
        else:
            pass  # Skip the label if it does not contain a wavelength

    # Save the peak data in app
    app.peak_data = peak_data



def open_help_document():
    # Define your markdown text
    markdown_text = """
# Help Section: Label Peaks

In this section, you can set the parameters that greatly impact the software's interpretation of the LIBS data. The tool gives you fine-grained control over how the program identifies and labels peaks in your spectral data. It consists of four sliders and three buttons for specific functionalities.

**1.  Change Font Size**: This slider controls the size of the text used in labels, helping you to tailor the visibility of information according to your preference.

**2.  Intensity Threshold**: This slider determines the intensity above which the program identifies and works on the spectral lines. The intensity threshold is relative to the intensity of the highest peak in the spectrum, with 100% equating to this peak intensity.

Moving the threshold higher will result in the software only processing spectral lines that are of high intensity, thus possibly reducing noise and focusing on the most significant elements in the sample. This could be particularly useful when the spectrometer's resolution is high, as it would help eliminate minor peaks that might be an artifact of the high-resolution data.

Conversely, lowering the threshold allows the software to consider smaller peaks, which could be beneficial when working with lower resolution data where important spectral lines might not be as intense. However, this might also increase the risk of misidentifying noise as meaningful data.

**3.  Prominence Filter**: This slider adds an additional filter that removes peaks based on their prominence relative to their local neighborhood. A peak must stand out by the specified percentage above its local baseline to be retained. For example, 15% means a peak must be at least 15% higher than the average intensity of nearby points.

This filter is particularly effective at removing small noise peaks that might pass the absolute intensity threshold but are not significant compared to their immediate surroundings. Set to 0% to disable this filter and use only the intensity threshold. Higher values (20-30%) provide more aggressive noise filtering, while lower values (5-15%) provide gentle noise reduction while preserving weak but significant peaks. For extremely noisy data, values up to 100-200% can be used to ensure only the most prominent peaks are retained.

**4.  Tolerance (nm)**: This slider defines how close (in nanometres) a detected peak must be to a database line for the peak to be tagged with that element and ionization level.

A wider tolerance can be useful when dealing with lower resolution spectrometers where the exact location of peaks might not be perfectly accurate. A tighter tolerance is beneficial when using high-resolution spectrometers that provide very accurate peak locations, allowing for more precise matching against the database.

### Additionally, there are three buttons for further control:

-   **Hide Unlabeled Peaks**: This button enables you to simplify your view by hiding all peaks that have not been labeled. This could help in focusing on the labeled peaks and reducing visual clutter.
-   **Delete Labels**: This button allows you to remove all labels from the spectral lines in one go. Use this when you want to start the labeling process afresh.
-   **Reduce Label Overlap**: If your spectrum is dense with peaks and labels, clicking this button will adjust the labels to minimize their overlap, making them easier to read and understand.

*Remember, adjusting these settings can significantly impact the results obtained from the LIBS data analysis. It's important to understand your spectrometer's specifications and the nature of your samples to fine-tune these parameters effectively.*

### Tips for Using the Prominence Filter:

- **Start with 15%**: This is a good default that removes most noise while preserving important peaks
- **Increase for noisy data**: Use 20-30% for very noisy spectra
- **Decrease for clean data**: Use 5-10% for high-quality spectra where you want to preserve weak peaks
- **Combine with intensity threshold**: Use both filters together for optimal results - the intensity threshold removes very small peaks, while the prominence filter removes local noise
- **Set to 0% to disable**: If you only want to use the intensity threshold, set the prominence filter to 0%
- **Extreme filtering (50-200%)**: For extremely noisy data or when you only want the most dominant peaks, use higher values. 100%+ means peaks must be at least double their local baseline
- **Fine-tuning approach**: Start low (15%), then gradually increase until unwanted noise disappears while keeping important spectral lines
"""

# Convert the markdown to HTML
    html_text = markdown.markdown(markdown_text)

    make_markdown_help_window(
        tk._default_root,
        "Peak Labeling Help",
        lambda: html_text,
        lambda parent, html: HTMLLabel(parent, html=html),
    )







