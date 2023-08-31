# calibration_free.py is a module that contains the functions for the calibration-free analysis of LIBS data.

import pandas as pd
import tkinter
import tkinter as tk
from tkinter import filedialog, Tk
from tkinter import Toplevel
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from ttkthemes import ThemedTk
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import linregress
from scipy.signal import find_peaks

# ======================================================================================================================
def calibration_free(app):
    # create root window and hide it
    root = Tk()
    root.withdraw()

    # open file dialog to select the csv file
    file_path = filedialog.askopenfilename()

    # load the csv into a pandas DataFrame
    df_final = pd.read_csv(file_path)

    # Preprocessing steps
    determine_electron_density(df_final)
    determine_saha_boltzmann_constants(df_final)

# ======================================================================================================================
    ### GUI Elements ###

    # Analysis and plotting
    summary_data = df_final.describe()  # Placeholder for actual summary data
    summary_window = tkinter.Toplevel()
    summary_window.title("Summary data")
    summary_window.geometry("1400x800") 

    # Adjust the root window grid configuration
    summary_window.grid_columnconfigure(0, weight=1)  # summary_frame column
    summary_window.grid_columnconfigure(1, weight=3)  # graph_frame column
    summary_window.grid_rowconfigure(0, weight=1)  # Add this line to configure the row weight

    # Create a frame for the summary data
    summary_frame = tkinter.Frame(summary_window)
    summary_frame.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)

    # Create a Scrollbar widget
    scrollbar = tkinter.Scrollbar(summary_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    # Create a Text widget with a vertical scrollbar, and set width to a desired value
    summary_text = tkinter.Text(summary_frame, yscrollcommand=scrollbar.set, width=30)
    summary_text.pack(fill=tk.BOTH, expand=True)

    # Configure the Scrollbar widget to scroll the Text widget
    scrollbar.config(command=summary_text.yview)

    # Create graph space for the Boltzmann plot
    graph_frame = tkinter.Frame(summary_window)
    graph_frame.grid(row=0, column=1, sticky='nsew')

    # create entry widgets for the weights
    signal_to_noise_weight_entry = tk.Entry(root)
    isolation_weight_entry = tk.Entry(root)
    sharpness_weight_entry = tk.Entry(root)

    # pack the entry widgets
    signal_to_noise_weight_entry.pack()
    isolation_weight_entry.pack()
    sharpness_weight_entry.pack()

    # get the weights from the entry widgets
    signal_to_noise_weight = float(signal_to_noise_weight_entry.get())
    isolation_weight = float(isolation_weight_entry.get())
    sharpness_weight = float(sharpness_weight_entry.get())

  
    #### Plot
    # Boltzmann plot
    fig, ax = plt.subplots(figsize=(4, 4), dpi=150)
    plt.subplots_adjust(left=0.1, right=0.9, top=0.9, bottom=0.1)  # Adjust the margins

    # Determine plasma temperatures and plot Boltzmann plots
    plasma_temperatures = determine_plasma_temperature(df_final, ax, summary_text)

    canvas = FigureCanvasTkAgg(fig, master=graph_frame)  # A tk.DrawingArea.
    canvas.draw()
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)  # Modify this line


    # Placeholder for the determined concentrations per element

    # ...

# ======================================================================================================================
# Boltzmann constant in eV/K
k = 8.6173e-5

def determine_plasma_temperature(df_final, ax, summary_text):
    # Create a dictionary to store the results
    plasma_temperatures = {}

    # Get the unique elements in the DataFrame
    elements = df_final['Symbol'].unique()

    # Sort the DataFrame by 'Symbol' and 'eUpper_x' columns
    df_final = df_final.sort_values(['Symbol', 'eUpper_x'])

    # Loop over each element
    for element in elements:
        # Filter the DataFrame for the current element
        df_element = df_final[df_final['Symbol'] == element]

        # Extract necessary columns
        I = df_element['Relative intensity']  # Unitless (It's a ratio)
        g = df_element['gUpper_x']  # Unitless (Statistical weight is a ratio)
        A = df_element['A (10^8 s^-1)_x']  # s^-1 (Transition probability is in inverse seconds)
        E = df_element['eUpper_x']  # eV (Energy is in electron volts)
        λ = df_element['Wavelength']  # m (Wavelength is in meters)
        ionization_level = df_element['Ionization Level'].iloc[0]

        # Calculate columnar number density
        N = I / (g * A * λ)

        # Calculate y and x values for the Boltzmann plot
        y = np.log(N)
        x = E

        # Fit a line to the Boltzmann plot
        slope, intercept, r_value, p_value, std_err = linregress(x, y)

        # Calculate plasma temperature and store it in the dictionary
        T = -1 / (k * slope)
        plasma_temperatures[element] = T

        # Plot the Boltzmann plot for the current element
        ax.plot(x, y, 'o', label=f'{element}, T = {T:.2f} K')
        ax.plot(x, intercept + slope*x, color=ax.lines[-1].get_color()) # match color with scatter plot
        
        # Write data to the summary panel
        summary_text.insert(tk.END, f'{element}, Ionization Level: {ionization_level}:\nPlasma Temperature = {T:.2f} K\n')
    
    ax.set_xlabel('Upper energy level E (eV)')
    ax.set_ylabel('ln(N)')
    ax.set_title('Columnar Density Saha–Boltzmann plot')
    ax.legend()

    print('Plasma temperatures (K):', plasma_temperatures)
    return plasma_temperatures

# ======================================================================================================================

def determine_stark_broadening():
    # Open a file dialog
    root = Tk()
    root.withdraw() # Hide the main window
    file_path = filedialog.askopenfilename() # Open the file dialog

    # Load the original LIBS spectra and element database
    df_spectra = pd.read_csv(file_path, names=['Wavelength', 'Intensity'], skiprows=1) # Assuming the first row contains headers
    df_elements = pd.read_csv('element_database.csv')

    # Round-off to no decimals for comparison
    df_spectra['Wavelength'] = df_spectra['Wavelength'].round(0)
    df_elements['Wavelength'] = df_elements['Wavelength'].round(0)

    # Find all peaks in the spectrum
    all_peaks, _ = find_peaks(df_spectra['Intensity'])

    # Filter the peaks based on whether their wavelength matches the wavelength of one of the elements of interest
    matching_peaks = [peak for peak in all_peaks if df_spectra.loc[peak, 'Wavelength'] in df_elements['Wavelength'].values]

    best_peak = None
    best_peak_score = -np.inf # Start with the lowest possible score

    # Iterate over matching peaks
    for peak in matching_peaks:
        # Calculate score for peak (add your scoring function here)
        peak_score = calculate_peak_score(peak, df_spectra, signal_to_noise_weight, isolation_weight, sharpness_weight)

        # If this peak is better than current best peak, update best peak
        if peak_score is not None and peak_score > best_peak_score:
            best_peak = peak
            best_peak_score = peak_score

    # Best peak info
    best_peak_info = df_spectra.loc[best_peak]

    return best_peak_info

def calculate_peak_score(peak, df_spectra, signal_to_noise_weight, isolation_weight, sharpness_weight, peak_width=10):
    """
    Calculate a score for a peak, based on several factors.
    The higher the score, the better the peak.

    Parameters:
    peak: The index of the peak in the dataframe.
    df_spectra: The dataframe containing the spectrum data.
    signal_to_noise_weight: The weight for the signal-to-noise ratio in the score.
    isolation_weight: The weight for the isolation in the score.
    sharpness_weight: The weight for the sharpness in the score.
    peak_width: The number of points on either side of the peak to consider when calculating isolation and sharpness.

    Returns:
    A score for the peak.
    """

    # Extract the peak intensity
    peak_intensity = df_spectra.loc[peak, 'Intensity']

    # Calculate the signal-to-noise ratio
    signal_to_noise_ratio = peak_intensity / df_spectra['Intensity'].std()

    # Calculate the isolation of the peak from other peaks
    isolation = min(df_spectra.loc[peak - peak_width : peak + peak_width, 'Intensity']) / peak_intensity

    # Calculate the sharpness of the peak
    sharpness = peak_intensity / df_spectra.loc[peak - 1 : peak + 1, 'Intensity'].mean()

    # Calculate the score as a weighted sum of the factors
    score = signal_to_noise_weight * signal_to_noise_ratio + isolation_weight * isolation + sharpness_weight * sharpness

    return score



# ======================================================================================================================

def determine_electron_density(df_final):
# code to determine electron density
    pass

# ======================================================================================================================
def determine_saha_boltzmann_constants(df_final):
    # code to determine Saha-Boltzmann Constants
    pass



