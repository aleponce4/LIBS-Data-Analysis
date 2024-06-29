import os
import pandas as pd
import tkinter as tk
from tkinter import ttk, Toplevel, filedialog, messagebox
from sklearn.linear_model import LinearRegression
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import style
plt.style.use('seaborn-whitegrid')

# Simplified import function
def import_calibration_data(app):
    filetypes = [("Txt files", "*.txt"),("CSV files", "*.csv"), ("All files", "*.*")]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes, parent=app.root)

    if not file_paths:
        return None

    all_data = pd.DataFrame()
    for idx, path in enumerate(file_paths):
        with open(path, 'r') as file:
            file_content = file.read()

        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        delimiter = '\t' if '\t' in file_content else ','
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)
        
        if all_data.empty:
            all_data = data
        else:
            # Ensure no duplicate columns by renaming the intensity column
            data.columns = [f'{col}_rep{idx+1}' if col != 0 else col for col in data.columns]
            all_data = pd.merge(all_data, data, on=0, how='outer')

    if not all_data.empty:
        all_data.columns = ['wavelength'] + [f'intensity_rep{idx+1}' for idx in range(len(file_paths))]
        return all_data
    else:
        messagebox.showerror("Error", "No data imported.")
        return None

# Function to calculate linearity
def calculate_linearity(intensities, concentrations):
    model = LinearRegression()
    intensities = np.array(intensities).reshape(-1, 1)
    concentrations = np.array(concentrations)
    model.fit(intensities, concentrations)
    r2 = model.score(intensities, concentrations)
    return r2, model

# Function to find the closest peak intensity within tolerance
def find_peak_intensity(wavelengths, intensities, target_wavelength, tolerance):
    mask = (wavelengths >= target_wavelength - tolerance) & (wavelengths <= target_wavelength + tolerance)
    if np.any(mask):
        return np.max(intensities[mask])  # Return the highest intensity within the tolerance range
    else:
        return None  # No peak found within the tolerance range

# Use the calibration equation to calculate concentrations
def calculate_concentrations(model, new_data, selected_peak):
    peak_wavelength = selected_peak['wavelength']
    tolerance = 0.5  # Define a tolerance for peak matching

    # Extract wavelengths from the new data
    wavelengths = new_data['wavelength']
    replicate_columns = [col for col in new_data.columns if col.startswith('intensity_rep')]

    results = []
    for idx, rep_col in enumerate(replicate_columns, start=1):
        intensities = new_data[rep_col]
        peak_intensity = find_peak_intensity(wavelengths, intensities, peak_wavelength, tolerance)
        if peak_intensity is not None:
            concentration = round(model.predict(np.array([[peak_intensity]]))[0], 2)
            results.append((f'Replicate {idx}', concentration))
        else:
            results.append((f'Replicate {idx}', np.nan))  # Mark as NaN if no peak found

    # Calculate error metrics
    concentrations = [res[1] for res in results if not np.isnan(res[1])]
    mean_concentration = round(np.mean(concentrations), 2)
    std_dev_concentration = round(np.std(concentrations), 2)
    rel_std_dev = round((std_dev_concentration / mean_concentration) * 100, 2) if mean_concentration != 0 else np.nan

    # Prepare the results table
    results_table = pd.DataFrame(results, columns=['Replicate', 'Concentration'])
    summary = pd.DataFrame([
        {'Replicate': 'Mean', 'Concentration': mean_concentration},
        {'Replicate': 'Std Dev', 'Concentration': std_dev_concentration},
        {'Replicate': 'RSD (%)', 'Concentration': rel_std_dev}
    ])
    results_table = pd.concat([results_table, summary], ignore_index=True)

    return results_table

# Function to display all results in one window
def display_results(element_data, selected_peak, new_data, results_table, model, r2):
    results_window = Toplevel()
    results_window.title("Calibration Results")

    # Plot the calibration curve
    fig, ax = plt.subplots()
    
    # Calibration data
    peak_data = element_data[element_data['wavelength'] == selected_peak['wavelength']]
    intensities = peak_data['relative_intensity']
    concentrations_calibration = peak_data['concentration']
    
    # Scatterplot for calibration data and linear fit
    ax.scatter(intensities, concentrations_calibration, label='Calibration Data', alpha=0.5, color="b")
    ax.plot(intensities, model.predict(np.array(intensities).reshape(-1, 1)), label='Linear Fit', color="r")
    
    # Labels and title
    ax.set_xlabel('Intensity')
    ax.set_ylabel('Concentration')
    ax.set_title(f'Calibration Curve for {selected_peak["wavelength"]} nm')
    ax.legend()
    ax.grid(True)

    # Plot the sample data points
    peak_wavelength = selected_peak['wavelength']
    wavelengths = new_data['wavelength']
    replicate_columns = [col for col in new_data.columns if col.startswith('intensity_rep')]
    
    for idx, rep_col in enumerate(replicate_columns, start=1):
        intensities = new_data[rep_col]
        # Find the index of the closest wavelength
        closest_idx = (np.abs(wavelengths - peak_wavelength)).idxmin()
        closest_intensity = intensities.iloc[closest_idx]
        concentration = results_table.loc[results_table['Replicate'] == f'Replicate {idx}', 'Concentration'].values[0]
        ax.scatter(closest_intensity, concentration, label=f'Samples', color = "b", alpha=0.5)
    
    # Display the plot in the window
    canvas = FigureCanvasTkAgg(fig, master=results_window)
    canvas.draw()
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

    # Display calibration information
    info_frame = ttk.Frame(results_window)
    info_frame.pack(side=tk.TOP, fill=tk.X, pady=5)
    
    slope = model.coef_[0]
    intercept = model.intercept_
    
    ttk.Label(info_frame, text=f"Slope: {slope:.4f}").grid(row=0, column=0, padx=5, pady=5)
    ttk.Label(info_frame, text=f"Intercept: {intercept:.4f}").grid(row=1, column=0, padx=5, pady=5)
    ttk.Label(info_frame, text=f"R²: {r2:.4f}").grid(row=2, column=0, padx=5, pady=5)

    # Display results table
    tree_frame = ttk.Frame(results_window)
    tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
    
    tree = ttk.Treeview(tree_frame, columns=("Replicate", "Concentration"), show='headings')
    tree.heading("Replicate", text="Replicate")
    tree.heading("Concentration", text="Concentration")
    
    for idx, row in results_table.iterrows():
        tree.insert("", "end", values=(row['Replicate'], row['Concentration']))
        if row['Replicate'] in ['Mean', 'Std Dev', 'RSD (%)']:
            tree.tag_configure('summary', font=('Helvetica', 10, 'bold'))
            tree.item(tree.get_children()[-1], tags='summary')

    tree.pack(fill=tk.BOTH, expand=True)

# Main Calibration curve function
def apply_calibration_curve(app):
    # Import new data for calibration curve
    new_data = import_calibration_data(app)
    if new_data is None:
        return

    # Read the calibration data
    calibration_file = "calibration_data_library.csv"
    if not os.path.exists(calibration_file):
        messagebox.showerror("Error", "Calibration data library not found.")
        return

    calibration_data = pd.read_csv(calibration_file)

    # Ask user to select an element
    element_window = Toplevel(app.root)
    element_window.title("Select Element for Calibration")

    ttk.Label(element_window, text="Select Element:").grid(row=0, column=0, padx=5, pady=5)
    element_var = tk.StringVar()
    element_dropdown = ttk.Combobox(element_window, textvariable=element_var)
    element_dropdown['values'] = sorted(set(calibration_data['element_symbol']))
    element_dropdown.grid(row=0, column=1, padx=5, pady=5)

    def proceed():
        selected_element = element_var.get()
        element_data = calibration_data[calibration_data['element_symbol'] == selected_element]
        
        # Calculate linearity for different peaks
        peaks = element_data['wavelength'].unique()
        linearity_data = []

        for peak in peaks:
            peak_data = element_data[element_data['wavelength'] == peak]
            intensities = peak_data['relative_intensity']
            concentrations = peak_data['concentration']
            r2, model = calculate_linearity(intensities, concentrations)
            linearity_data.append([peak, r2])

        linearity_df = pd.DataFrame(linearity_data, columns=["wavelength", "r2"])

        # Create a new window to display linearity data
        linearity_window = Toplevel(app.root)
        linearity_window.title("Select Peak for Best Linearity")

        # Frame for table and checkboxes
        peaks_frame = ttk.Frame(linearity_window)
        peaks_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

        # Create canvas to hold checkboxes and treeview
        canvas = tk.Canvas(peaks_frame)
        canvas.grid(row=0, column=0, sticky="nsew")

        # Scrollbars for the canvas
        scrollbar_y = ttk.Scrollbar(peaks_frame, orient='vertical', command=canvas.yview)
        scrollbar_y.grid(row=0, column=1, sticky='ns')
        scrollbar_x = ttk.Scrollbar(peaks_frame, orient='horizontal', command=canvas.xview)
        scrollbar_x.grid(row=1, column=0, sticky='ew')

        canvas.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        # Create a frame inside the canvas
        inner_frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner_frame, anchor="nw")

        # Create a list to store the BooleanVars
        check_vars = []

        # Add header labels with padding for r2 column
        ttk.Label(inner_frame, text="Select", font=('Helvetica', 10, 'bold')).grid(row=0, column=0, padx=5, pady=5)
        ttk.Label(inner_frame, text="Wavelength (nm)", font=('Helvetica', 10, 'bold')).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(inner_frame, text="Linearity (R²)", font=('Helvetica', 10, 'bold')).grid(row=0, column=2, padx=(20, 5), pady=5)

        for idx, row in linearity_df.iterrows():
            check_var = tk.BooleanVar()
            check_vars.append(check_var)
            ttk.Checkbutton(inner_frame, variable=check_var).grid(row=idx + 1, column=0, sticky="w")
            ttk.Label(inner_frame, text=row['wavelength']).grid(row=idx + 1, column=1)
            ttk.Label(inner_frame, text=f"{row['r2']:.2f}").grid(row=idx + 1, column=2, padx=(20, 5))

        inner_frame.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

        def save_selected_peak():
            selected_peak = None
            for i, check_var in enumerate(check_vars):
                if check_var.get():
                    selected_peak = linearity_df.iloc[i]
                    break
            if selected_peak is not None:
                messagebox.showinfo("Selected Peak", f"Selected peak: {selected_peak['wavelength']} nm with R²: {selected_peak['r2']:.4f}")

                # Calculate concentrations for new data
                results_table = calculate_concentrations(model, new_data, selected_peak)

                # Display all results in one window
                display_results(element_data, selected_peak, new_data, results_table, model, selected_peak['r2'])

            linearity_window.destroy()

        # Save button
        save_button = ttk.Button(linearity_window, text="Save", command=save_selected_peak)
        save_button.grid(row=2, column=0, columnspan=2, pady=10)

    proceed_button = ttk.Button(element_window, text="Proceed", command=proceed)
    proceed_button.grid(row=1, column=0, columnspan=2, pady=10)

    element_window.mainloop()
