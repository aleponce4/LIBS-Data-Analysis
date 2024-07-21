import pandas as pd
import numpy as np
from sklearn.metrics import r2_score
from tkinter import Toplevel, ttk, messagebox
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import seaborn as sns
from scipy.signal import savgol_filter
from scipy.sparse import diags, eye
from scipy.sparse import linalg as splinalg
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.ensemble import GradientBoostingRegressor

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

    # Add a unique identifier for each replicate to handle duplicates
    element_data['replicate_id'] = element_data.groupby(['concentration', 'wavelength']).cumcount()

    # Prepare the data for regression
    intensity_matrix = element_data.pivot(index=['concentration', 'replicate_id'], columns='wavelength', values='relative_intensity').fillna(0)

    # Extract the concentration values
    concentration_values = intensity_matrix.index.get_level_values('concentration').astype(float).values

    # Ensure consistent lengths
    if intensity_matrix.shape[0] != len(concentration_values):
        messagebox.showerror("Error", "Inconsistent data lengths between concentrations and intensity matrix.")
        return

    intensity_matrix_values = intensity_matrix.values

    # Standardize the data
    scaler = StandardScaler()
    intensity_matrix_scaled = scaler.fit_transform(intensity_matrix_values)

    # Perform Gradient Boosting Regression
    gb = GradientBoostingRegressor(n_estimators=100, random_state=42)
    gb.fit(intensity_matrix_scaled, concentration_values)
    predictions = gb.predict(intensity_matrix_scaled)
    r2 = r2_score(concentration_values, predictions)

    print(f'Gradient Boosting Model R2: {r2:.3f}')



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

    # Align the wavelengths between calibration and prediction datasets
    common_wavelengths = intensity_matrix.columns.intersection(replicate_data['Wavelength'])

    intensity_matrix = intensity_matrix[common_wavelengths]
    replicate_data = replicate_data[replicate_data['Wavelength'].isin(common_wavelengths)]

    # Melt the replicate_data DataFrame to long format
    replicate_data_melted = replicate_data.melt(id_vars=['Wavelength'], var_name='Sample', value_name='Intensity')

    # Print the columns of replicate_data for debugging
    print("Columns in replicate_data_melted:", replicate_data_melted.columns)

    # Pivot the melted DataFrame
    replicate_intensity_matrix = replicate_data_melted.pivot(index='Wavelength', columns='Sample', values='Intensity').fillna(0).values

    # Standardize the replicate intensity matrix
    replicate_intensity_matrix_scaled = scaler.transform(replicate_intensity_matrix.T)
    predicted_concentrations = gb.predict(replicate_intensity_matrix_scaled)

    # Plotting and Showing Results
    result_window = Toplevel(app.root)
    result_window.title("Calibration Results")
    result_window.geometry("1000x800")

    fig, ax = plt.subplots()

    # Plot calibration points and regression line
    sns.scatterplot(x=concentration_values, y=predictions, ax=ax, label='Calibration Points', s=50)
    sns.lineplot(x=concentration_values, y=predictions, ax=ax, label=f'Fit Line (R2: {r2:.2f})')
    sns.scatterplot(x=predicted_concentrations, y=gb.predict(replicate_intensity_matrix_scaled), ax=ax, label='New Samples', s=50)

    ax.set_xlabel('Concentration')
    ax.set_ylabel('Predicted Concentration')
    ax.legend()
    ax.set_title(f'Gradient Boosting regression: {selected_element}')

    # Display the plot in the Tkinter window
    canvas = FigureCanvasTkAgg(fig, master=result_window)
    canvas.draw()
    canvas.get_tk_widget().pack(fill='both', expand=True)

    # Create a frame for the table and metrics
    frame = ttk.Frame(result_window)
    frame.pack(fill='both', expand=True)

    # Calculate metrics for the new samples
    avg_concentration = np.mean(predicted_concentrations)
    std_deviation = np.std(predicted_concentrations)

    # Create a table for the calculated concentrations
    table_frame = ttk.Frame(frame)
    table_frame.pack(fill='both', expand=True)
    table = ttk.Treeview(table_frame, columns=('Sample', 'Concentration'), show='headings')
    table.heading('Sample', text='Sample')
    table.heading('Concentration', text='Concentration')
    table.column('Sample', anchor='center', width=100)
    table.column('Concentration', anchor='center', width=150)

    # Add rows for calculated concentrations
    for i, concentration in enumerate(predicted_concentrations):
        table.insert('', 'end', values=(i + 1, f'{concentration:.3f}'))

    # Add an empty row as a divider
    table.insert('', 'end', values=('', ''), tags=('divider',))

    # Add rows for metrics
    table.insert('', 'end', values=('Average', f'{avg_concentration:.3f}'), tags=('metric',))
    table.insert('', 'end', values=('Std Dev', f'{std_deviation:.3f}'), tags=('metric',))

    # Apply styles for the rows
    style = ttk.Style()
    style.configure('Treeview', rowheight=25)
    style.configure('Treeview.Heading', font=('Calibri', 12, 'bold'))
    style.configure('Treeview', font=('Calibri', 12))
    style.map('Treeview', background=[('selected', 'blue')])

    # Configure the treeview tags directly
    table.tag_configure('metric', foreground='black')

    table.pack(fill='both', expand=True)

    # Add a scrollbar to the table
    scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
    table.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')



