# Description: This file contains the label_peaks function, which is used to label the peaks in the spectrum.

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


# Labels the peaks in the spectrum:
def label_peaks(app, ax, element_df):

    #### Graphical part of the function, creates a window, scale and button ####
    # Define the round-off error tolerance window:
    def tolerance_window(app, ax, element_df):
        tolerance_window = tk.Toplevel(app.root)
        tolerance_window.title("Set Treshold and Round-off Error Tolerance")

        # Create the Font Size slider ##########################################################
        ttk.Label(tolerance_window, text="Font size:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        font_size_var = tk.IntVar()
        font_size_var.set(8)
        font_size_slider = ttk.Scale(tolerance_window, from_=5, to=20, orient=tk.HORIZONTAL, variable=font_size_var, length=300)
        font_size_slider.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Function to update slider label
        def update_font_size_slider_label(app, ax, font_size_var):
            font_size_slider_label.config(text=f"{font_size_var.get()}")
            update_labels(app, ax, intensity_var, round_off_error_tolerance_var, font_size_var)

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

        # Function to update slider 
        def update_slider_label(app, ax, intensity_var, round_off_error_tolerance_var):
            slider_label.config(text=f"{intensity_var.get()}%")
            update_labels(app, ax, intensity_var, round_off_error_tolerance_var, font_size_var)

        intensity_var.trace("w", lambda *args: update_slider_label(app, ax, intensity_var, round_off_error_tolerance_var))

        # Create the intensity threshold slider label
        slider_label = ttk.Label(tolerance_window, text=f"{intensity_var.get()}%")
        slider_label.grid(row=1, column=2, columnspan=2, padx=15, pady=15, sticky="w")

        # Create the round-off error tolerance slider  ##########################################
        ttk.Label(tolerance_window, text="Select Tolerance (nm)").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        round_off_error_tolerance_var = tk.IntVar()
        round_off_error_tolerance_var.set(0)
        round_off_error_tolerance_slider = ttk.Scale(tolerance_window, from_=0, to=3, orient=tk.HORIZONTAL, variable=round_off_error_tolerance_var, length=300)
        round_off_error_tolerance_slider.grid(row=2, column=1, padx=5, pady=5, sticky="w")
        app.round_off_error_tolerance_var = round_off_error_tolerance_var

        # Function to update slider label with decimal values
        def update_round_off_error_tolerance_slider_label(app, ax, intensity_var, round_off_error_tolerance_slider_label, round_off_error_tolerance_var):
            # Calculate decimal value based on current slider value
            decimal_value = round(10 ** (-round_off_error_tolerance_var.get()), 3)
            # Update slider label with decimal value
            round_off_error_tolerance_slider_label.config(text=f"{decimal_value}")
            # Update plot labels
            update_labels(app, ax, intensity_var, round_off_error_tolerance_var, font_size_var)

        # Update the slider label when the slider is moved
        round_off_error_tolerance_var.trace("w", lambda *args: update_round_off_error_tolerance_slider_label(app, ax, intensity_var, round_off_error_tolerance_slider_label, round_off_error_tolerance_var))

        # Create the round-off error tolerance slider label
        round_off_error_tolerance_slider_label = ttk.Label(tolerance_window, text=f"{round_off_error_tolerance_var.get()}")
        round_off_error_tolerance_slider_label.grid(row=2, column=2, padx=15, pady=15, sticky="w")


        ################ Buttons ################
        buttons_frame = ttk.LabelFrame(tolerance_window)
        buttons_frame.grid(row=4, column=0, columnspan=7, padx=5, pady=5)

        # Set the same weight for each column in the buttons_frame
        buttons_frame.grid_columnconfigure(0, weight=1)
        buttons_frame.grid_columnconfigure(1, weight=1)
        buttons_frame.grid_columnconfigure(2, weight=1)
        buttons_frame.grid_columnconfigure(3, weight=1)

        # Create the Hide unlabeled peaks toggle button
        hide_unlabeled_peaks_var = tk.BooleanVar()
        hide_unlabeled_peaks_button = ttk.Checkbutton(buttons_frame, text="Hide unlabeled peaks", variable=hide_unlabeled_peaks_var, command=lambda: toggle_unlabeled_peaks(app, ax, hide_unlabeled_peaks_var), style="Toggle.TButton")
        hide_unlabeled_peaks_button.grid(row=0, column=0, sticky='ew', padx=5, pady=5)

        # Create the Erase labels button
        erase_button = ttk.Button(buttons_frame, text="Delete Labels", command=lambda: delete_labels(app, ax))
        erase_button.grid(row=0, column=1, sticky='ew', padx=5, pady=5)

        # Create the Reduce overlap button
        apply_button = ttk.Button(buttons_frame, text="Reduce label overlap", command=lambda: adjust_labels(app, ax, intensity_var, font_size_var, time_lim=3))
        apply_button.grid(row=0, column=2, sticky='ew', padx=5, pady=5)

        # Add the Help button
        help_button = ttk.Button(buttons_frame, text="Help", command=open_help_document)
        help_button.grid(row=0, column=3, sticky='ew', padx=5, pady=5)

        # Create the Close button
        close_button = ttk.Button(buttons_frame, text="Close", command=tolerance_window.destroy)
        close_button.grid(row=0, column=4, sticky='ew', padx=5, pady=5)

        update_labels(app, ax, intensity_var, round_off_error_tolerance_var, font_size_var)

    # call the tolerance_window function
    tolerance_window(app, ax, element_df)

# ================================================================================================
# Update labels function
def update_labels(app, ax, intensity_var, round_off_error_tolerance_var, font_size_var):  # Add round_off_error_tolerance_var to the function arguments
    # Update the intensity threshold
    app.current_threshold_percent = intensity_var.get()
    app.round_off_error_tolerance = round_off_error_tolerance_var.get()  # Update round-off error tolerance from the argument
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

     # Label the peaks with element symbols and ionization levels if possible
    texts = []  # store text objects for adjust_text
    for idx in peak_indices[0]:
        if y_data[idx] >= threshold:
            label = f"{x_data[idx]:.3f} nm"
            
            # Check if element_df is not None before accessing it
            if element_df is not None:
                # Get the current peak wavelength
                peak_wavelength = x_data[idx]
                # Get the tolerance value from the slider (in nm)
                tolerance = app.round_off_error_tolerance_var.get()  # This will replace the decimal rounding mechanism
                # Convert the Wavelength column to numeric values
                element_df.loc[:, "Wavelength"] = pd.to_numeric(element_df["Wavelength"], errors="coerce")
                # Find the closest match within the tolerance range
                closest_match = element_df.loc[
                    (element_df["Wavelength"] >= peak_wavelength - tolerance) & 
                    (element_df["Wavelength"] <= peak_wavelength + tolerance)
                ]

                # If a match is found, label it with the element symbol and ionization level
                if not closest_match.empty:
                    match_idx = closest_match.index[0]  # Get the index of the first match
                    element_symbol = closest_match.iloc[0]["Symbol"]
                    ionization_level = closest_match.iloc[0]["Ionization Level"]
                    label += f" ({element_symbol}, {ionization_level})"


            # Add the following two lines to create the text object and append it to the texts list
            text_obj = ax.text(x_data[idx], y_data[idx], label, fontsize=font_size_var.get(), ha='center', va='bottom')
            texts.append(text_obj)

    # Update app.peak_labels with the adjusted text objects
    app.peak_labels = texts

    # Save peak values
    save_peak_values(app)

    # Extract peak info after labeling the peaks
    extract_peak_info(app)

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
    # Remove existing lines from the axes matplotlib
    for line in ax.lines[1:]:
        line.remove()

    for text_obj in ax.texts:
        text_obj.remove()

    # Remove existing peak labels
    for text_obj in app.peak_labels:
        text_obj.remove()
    app.peak_labels = []
    app.peak_values = []

    # Redraw the canvas
    app.canvas.draw()

#================================================================================================
def adjust_labels(app, ax, intensity_var, font_size_var, time_lim=3):
    threshold = app.current_threshold_percent / 100.0 * max(app.peak_values)

    # Filter labels based on visibility and threshold
    filtered_labels = [(text_obj, peak_value) for text_obj, peak_value in zip(app.peak_labels, app.peak_values) if text_obj.get_visible() and peak_value >= threshold]

    x = [text_obj.get_position()[0] for text_obj, _ in filtered_labels]
    y = [text_obj.get_position()[1] for text_obj, _ in filtered_labels]
    text_list = [text_obj.get_text() for text_obj, _ in filtered_labels]

    # Adjust the labels
    ta.allocate_text(ax.figure, ax, x, y, text_list, x_scatter=x, y_scatter=y, textsize=font_size_var.get(), linecolor="black")


    # Re-create and store the adjusted labels in app.peak_labels
    for i, (x_val, y_val, text) in enumerate(zip(x, y, text_list)):
        text_obj = ax.text(x_val, y_val, text, fontsize=font_size_var.get(), ha='center', va='bottom')
        app.peak_labels.append(text_obj)

    # Call erase_labels function to remove existing labels
    erase_labels(app, ax)

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
    #print("Extracting peak info from these labels:", [text_obj.get_text() for text_obj in app.peak_labels])  # Debug print

    peak_data = []
    for text_obj in app.peak_labels:
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
                relative_intensity = text_obj.get_position()[1]
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
# Help Section: Set Threshold and Round-off Error Tolerance

In this section, you can set the parameters that greatly impact the software's interpretation of the LIBS data. The tool gives you fine-grained control over how the program identifies and labels peaks in your spectral data. It consists of three sliders and three buttons for specific functionalities.

**1.  Change Font Size**: This slider controls the size of the text used in labels, helping you to tailor the visibility of information according to your preference.
**2.  Intensity Threshold**: This slider determines the intensity above which the program identifies and works on the spectral lines. The intensity threshold is relative to the intensity of the highest peak in the spectrum, with 100% equating to this peak intensity.

Moving the threshold higher will result in the software only processing spectral lines that are of high intensity, thus possibly reducing noise and focusing on the most significant elements in the sample. This could be particularly useful when the spectrometer's resolution is high, as it would help eliminate minor peaks that might be an artifact of the high-resolution data.

Conversely, lowering the threshold allows the software to consider smaller peaks, which could be beneficial when working with lower resolution data where important spectral lines might not be as intense. However, this might also increase the risk of misidentifying noise as meaningful data.

**1.  Round-off Error Tolerance**: This slider defines how precise the software is when comparing the peaks for matches in the database. It allows for a range from rounding database and peaks to no decimals, to making the comparison with a tolerance of 0.001.

A higher round-off error tolerance (i.e., less precision) can be useful when dealing with lower resolution spectrometers where the exact location of peaks might not be perfectly accurate. A lower tolerance (more precision) is beneficial when using high-resolution spectrometers that provide very accurate peak locations, allowing for more precise matching against the database.

### Additionally, there are three buttons for further control:

-   **Hide Unlabeled Peaks**: This button enables you to simplify your view by hiding all peaks that have not been labeled. This could help in focusing on the labeled peaks and reducing visual clutter.
-   **Delete All Labels**: This button allows you to remove all labels from the spectral lines in one go. Use this when you want to start the labeling process afresh.
-   **Reduce Label Overlap**: If your spectrum is dense with peaks and labels, clicking this button will adjust the labels to minimize their overlap, making them easier to read and understand.

*Remember, adjusting these settings can significantly impact the results obtained from the LIBS data analysis. It's important to understand your spectrometer's specifications and the nature of your samples to fine-tune these parameters effectively.
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







