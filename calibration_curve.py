import os
import pandas as pd
import tkinter as tk
from tkinter import ttk, Toplevel, filedialog, messagebox
from sklearn.linear_model import LinearRegression
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import style
import matplotlib.ticker as ticker
import statsmodels.api as sm
plt.style.use('seaborn-whitegrid')


def write_received_data_to_file(element, concentration, units, spectra_data, filename="received_data.txt"):
    with open(filename, 'w') as file:
        file.write(f"Element: {element}\n")
        file.write(f"Concentration: {concentration}\n")
        file.write(f"Units: {units}\n")
        file.write("Spectra Data:\n")
        spectra_data.to_csv(file, index=False)

    print(f"Data written to {filename}")

# Example function call
def process_data(element, concentration, units, spectra_data):
    # Write data to file for verification
    write_received_data_to_file(element, concentration, units, spectra_data)


def calibration_curve(element, concentration, units, spectra_data):
    # Placeholder implementation
    print(f"Element: {element}")
    print(f"Concentration: {concentration}")
    print(f"Units: {units}")
    print("Spectra Data:")
    print(spectra_data)
    
    # Return a placeholder result
    return None

def apply_calibration_curve(app):
    # Placeholder implementation
    print("apply_calibration_curve called with app:", app)
    
    # Perform a minimal action or return a placeholder result
    return None
