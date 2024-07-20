import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score, mean_squared_error
import tkinter as tk
from tkinter import Toplevel, ttk, messagebox
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from scipy.signal import savgol_filter
from scipy.sparse import diags, eye
from scipy.sparse import linalg as splinalg
from sklearn.preprocessing import MinMaxScaler


########## Determine best model for calibration curve
def process_calibration(app):
    selected_element = app.selected_element
    replicate_data = app.replicate_data

    # Load the calibration data library
    try:
        calibration_df = pd.read_csv('calibration_data_library.csv')
    except FileNotFoundError:
        messagebox.showerror("Error", "Calibration data library not found.")
        return

    # Filter the calibration data for the selected element
    element_data = calibration_df[calibration_df['element_symbol'] == selected_element]

    if element_data.empty:
        messagebox.showerror("Error", f"No calibration data found for element {selected_element}.")
        return

    # Initialize variables to track the best fit
    best_r2 = -np.inf
    best_wavelength = None
    best_slope = None

    # Get unique wavelengths for the selected element
    unique_wavelengths = element_data['wavelength'].unique()

    # Function to perform linear regression and calculate R2
    def perform_linear_regression(concentrations, intensities):
        A = np.hstack([concentrations, np.ones_like(concentrations)])
        coeffs, _, _, _ = np.linalg.lstsq(A, intensities, rcond=None)
        slope = coeffs[0]
        intercept = coeffs[1]
        predictions = A @ coeffs
        r2 = r2_score(intensities, predictions)
        return slope, r2

    for wavelength in unique_wavelengths:
        wavelength_data = element_data[element_data['wavelength'] == wavelength]
        concentrations = wavelength_data['concentration'].astype(float).values.reshape(-1, 1)
        intensities = wavelength_data['relative_intensity'].astype(float).values
        
        # Perform linear regression and calculate R2
        slope, r2 = perform_linear_regression(concentrations, intensities)
        
        # Update the best fit if the current one is better and the slope is positive
        if r2 > best_r2:
            best_r2 = r2
            best_wavelength = wavelength
            best_slope = slope

    if best_wavelength is None:
        messagebox.showerror("Error", "No suitable peak found for calibration.")
        return
    

    ###### Predicting Concentrations ######
    # Preprocessing functions
    def baseline_correction(y, lam=1e5, p=0.01, niter=10, eps=1e-6):
        L = len(y)
        D = diags([1, -2, 1], [0, 1, 2], shape=(L-2, L)).tocsc()
        H = lam * (D.T @ D)
        w = np.ones(L)
        for i in range(niter):
            W = diags(w, 0).tocsc()
            try:
                Z = splinalg.spsolve(W + H + eps * eye(L), W @ y)
            except splinalg.ArpackNoConvergence:
                return y  # Return original data in case of error
            w = p * (y > Z) + (1 - p) * (y < Z)
        return Z

    def preprocess_spectrum(df, window_length=11, polyorder=2, baseline_lambda=1e5, baseline_p=0.01, baseline_niter=10, eps=1e-6):
        df_preprocessed = df.copy()
        for col in df.columns[1:]:
            y = df[col].values
            baseline = baseline_correction(y, lam=baseline_lambda, p=baseline_p, niter=baseline_niter, eps=eps)
            corrected = y - baseline
            smoothed = savgol_filter(corrected, window_length, polyorder)
            df_preprocessed[col] = smoothed
        return df_preprocessed

    def normalize_data(df):
        df_normalized = df.copy()
        scaler = MinMaxScaler()
        for col in df.columns[1:]:
            df_normalized[col] = scaler.fit_transform(df[[col]])
        return df_normalized

    # Preprocess and normalize the replicate data
    replicate_data = preprocess_spectrum(replicate_data)
    replicate_data = normalize_data(replicate_data)

    # Select intensities for the best peak
    intensities = replicate_data[replicate_data['Wavelength'] == best_wavelength].iloc[:, 1:].values.flatten()

    # Get concentrations from the calibration data
    concentrations = element_data[element_data['wavelength'] == best_wavelength]['concentration'].astype(float).values

    # Perform linear regression
    concentrations = concentrations.reshape(-1, 1)
    model = LinearRegression()
    model.fit(concentrations, intensities)
    predictions = model.predict(concentrations)
    r2 = r2_score(intensities, predictions)
    slope = model.coef_[0]
    intercept = model.intercept_


    ###### Plotting and Report ######
    # Create a new window for displaying the plot and table
    result_window = Toplevel(app.root)
    result_window.title("Calibration Results")
    result_window.geometry("1200x800")

    # Create a figure for the plot
    fig, ax = plt.subplots()

    # Plot calibration points and regression line
    ax.scatter(concentrations, intensities, color='blue', label='Calibration Points')
    ax.plot(concentrations, predictions, color='red', label=f'Fit Line (R2: {r2:.2f})')
    ax.set_xlabel('Concentration')
    ax.set_ylabel('Intensity')
    ax.legend()
    ax.grid(True)

    # Create a canvas to display the plot in the tkinter window
    canvas = FigureCanvasTkAgg(fig, master=result_window)
    canvas.draw()
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # Create a frame for the table and metrics
    frame = ttk.Frame(result_window)
    frame.pack(fill=tk.BOTH, expand=True)

    # Calculate metrics for the new samples
    new_intensities = intensities[len(concentrations):]
    new_concentrations = (new_intensities - intercept) / slope
    avg_concentration = np.mean(new_concentrations)
    std_deviation = np.std(new_concentrations)

    # Create a table for the calculated concentrations
    table_frame = ttk.Frame(frame)
    table_frame.pack(fill=tk.BOTH, expand=True)
    table = ttk.Treeview(table_frame, columns=('Sample', 'Concentration'), show='headings')
    table.heading('Sample', text='Sample')
    table.heading('Concentration', text='Concentration')
    for i, concentration in enumerate(new_concentrations):
        table.insert('', 'end', values=(i + 1, concentration))
    table.pack(fill=tk.BOTH, expand=True)

    # Display metrics below the table
    metrics_frame = ttk.Frame(frame)
    metrics_frame.pack(fill=tk.X)
    ttk.Label(metrics_frame, text=f'Average Concentration: {avg_concentration:.2f}').pack(side=tk.LEFT, padx=10)
    ttk.Label(metrics_frame, text=f'Standard Deviation: {std_deviation:.2f}').pack(side=tk.LEFT, padx=10)






