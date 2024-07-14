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


# Simplified import function
def import_calibration_data(app):
    filetypes = [("Txt files", "*.txt"),("CSV files", "*.csv"), ("All files", "*.*")]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes, parent=app.root)

    if not file_paths:
        return None

    all_data = pd.DataFrame()
    for idx, path in enumerate(file_paths):
        with open(path, 'r') as file:
            file_content = file.read().strip()  # Remove leading/trailing whitespace
        
        print(f"First few lines of file {idx+1} content:\n{file_content.splitlines()[:5]}")

        # Detect decimal separator and delimiter
        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        if '\t' in file_content:
            delimiter = '\t'
        elif ',' in file_content:
            delimiter = ','
        else:
            delimiter = '\s+'  # Handle whitespace-delimited data

        # Read the data into a DataFrame, skipping the first row if it contains metadata
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)

        # Handle potential extra columns
        if data.shape[1] > 2:
            print(f"Warning: More than 2 columns detected in file {path}. Trimming to the first two columns.")
            data = data.iloc[:, :2]

        if data.shape[1] == 2:
            data.columns = ['wavelength', f'intensity_rep{idx+1}']
        else:
            raise ValueError(f"Expected 2 columns but got {data.shape[1]} in file: {path}")

        if all_data.empty:
            all_data = data
        else:
            all_data = pd.merge(all_data, data, on='wavelength', how='outer')

        print(f"Columns after importing file {idx+1}: {all_data.columns}")

    if not all_data.empty:
        print(f"Final columns after merging: {all_data.columns}")
        return all_data
    else:
        messagebox.showerror("Error", "No data imported.")
        return None



# Function to calculate linearity with confidence intervals
def calculate_linearity(intensities, concentrations):
    intensities = pd.Series(intensities)  # Convert intensities to a Pandas Series
    concentrations = pd.Series(concentrations)  # Convert concentrations to a Pandas Series

    if len(concentrations.unique()) < 2:
        raise ValueError("Insufficient data points for regression")

    unique_concentrations = concentrations.unique()
    
    mean_intensities = intensities.groupby(concentrations).mean()
    std_intensities = intensities.groupby(concentrations).std()

    mean_intensities = mean_intensities.loc[unique_concentrations].values.reshape(-1, 1)
    std_intensities = std_intensities.loc[unique_concentrations].values

    model = sm.OLS(unique_concentrations, sm.add_constant(mean_intensities)).fit()
    r2 = model.rsquared

    # Calculate confidence intervals for mean intensities with tighter intervals (increase alpha)
    predictions = model.get_prediction(sm.add_constant(mean_intensities))
    prediction_summary = predictions.summary_frame(alpha=0.05)  # Adjusted alpha for narrower intervals
    
    return r2, model, prediction_summary, mean_intensities, std_intensities



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
            # Ensure the input includes the constant term
            prediction_input = np.array([[1, peak_intensity]])  # Adding the constant term explicitly
            concentration = round(model.predict(prediction_input)[0], 2)
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
def display_results(element_data, selected_peak, new_data, results_table, model, r2, prediction_summary, mean_intensities, std_intensities, selected_element):
    results_window = Toplevel()
    results_window.title("Calibration Results")
    results_window.geometry("1200x800")

    # Plot the calibration curve
    fig, ax = plt.subplots()
    fig.subplots_adjust(right=0.7)

    # Calibration data
    peak_data = element_data[element_data['wavelength'] == selected_peak['wavelength']]
    concentrations_calibration = peak_data['concentration']

    print(f"Mean Intensities: {mean_intensities.flatten()}")
    print(f"Std Intensities: {std_intensities}")

    # Ensure that the correct values are used for plotting
    ax.scatter(mean_intensities.flatten(), concentrations_calibration.unique(), label='Calibration Data', alpha=0.5, color="b")
    ax.plot(mean_intensities.flatten(), model.predict(sm.add_constant(mean_intensities)), label='Linear Fit', color="steelblue")
    print(f"Calibration Data Points: {list(zip(mean_intensities.flatten(), concentrations_calibration.unique(), std_intensities))}")

    # Print shapes to debug the fill_between issue
    print(f"mean_intensities.shape: {mean_intensities.shape}")
    print(f"prediction_summary['mean_ci_lower'].shape: {prediction_summary['mean_ci_lower'].shape}")
    print(f"prediction_summary['mean_ci_upper'].shape: {prediction_summary['mean_ci_upper'].shape}")

    # Plot confidence bands with transparency
    ax.fill_between(
        mean_intensities.flatten(),
        prediction_summary['mean_ci_lower'],
        prediction_summary['mean_ci_upper'],
        color='lightblue',
        alpha=0.3,  # Adding transparency
        label='Confidence Interval'
    )

    # Labels and title
    ax.set_xlabel('Intensity')
    ax.set_ylabel('Concentration')
    ax.set_title(f'Calibration Curve for {selected_peak["wavelength"]} nm - {selected_element}')
    ax.grid(True)

    # Set x-axis to scientific notation
    formatter = ticker.ScalarFormatter(useMathText=True)
    formatter.set_scientific(True)
    formatter.set_powerlimits((-2, 2))  # Adjust power limits as needed
    ax.xaxis.set_major_formatter(formatter)

    # Plot the sample data points
    peak_wavelength = selected_peak['wavelength']
    wavelengths = new_data['wavelength']
    replicate_columns = [col for col in new_data.columns if col.startswith('intensity_rep')]

    first_label = True
    for idx, rep_col in enumerate(replicate_columns, start=1):
        intensities = new_data[rep_col]
        # Find the index of the closest wavelength
        closest_idx = (np.abs(wavelengths - peak_wavelength)).idxmin()
        closest_intensity = intensities.iloc[closest_idx]
        concentration = results_table.loc[results_table['Replicate'] == f'Replicate {idx}', 'Concentration'].values[0]
        print(f"Sample Data Point: (Intensity: {closest_intensity}, Concentration: {concentration})")
        if first_label:
            ax.scatter(closest_intensity, concentration, label='Samples', color='black', alpha=0.5)
            first_label = False
        else:
            ax.scatter(closest_intensity, concentration, color='black', alpha=0.5)

    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))

    # Display the plot in the window
    canvas = FigureCanvasTkAgg(fig, master=results_window)
    canvas.draw()
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

    # Display calibration information
    info_frame = ttk.Frame(results_window)
    info_frame.pack(side=tk.TOP, fill=tk.X, pady=5)

    slope = model.params[1]
    intercept = model.params[0]

    ttk.Label(info_frame, text=f"Slope: {slope:.4f}").grid(row=0, column=0, padx=5, pady=5)
    ttk.Label(info_frame, text=f"Intercept: {intercept:.4f}").grid(row=1, column=0, padx=5, pady=5)
    ttk.Label(info_frame, text=f"R²: {r2:.4f}").grid(row=2, column=0, padx=5, pady=5)

    # Display results table
    tree_frame = ttk.Frame(results_window)
    tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # Define columns
    columns = ("Replicate", "Concentration")
    tree = ttk.Treeview(tree_frame, columns=columns, show='headings')

    # Define headings
    tree.heading("Replicate", text="Replicate")
    tree.heading("Concentration", text="Concentration")

    # Format data and insert rows
    for idx, row in results_table.iterrows():
        # Format concentration to 3 decimal points
        formatted_concentration = f"{row['Concentration']:.3f}" if pd.notnull(row['Concentration']) else 'NaN'
        tree.insert("", "end", values=(row['Replicate'], formatted_concentration))
        if row['Replicate'] in ['Mean', 'Std Dev', 'RSD (%)']:
            tree.tag_configure('summary', font=('Helvetica', 10, 'bold'))
            tree.item(tree.get_children()[-1], tags='summary')

    # Add lines to separate replicates from summary statistics
    tree.insert("", "end", values=("", ""))  # Add an empty row for separation

    # Pack treeview
    tree.pack(fill=tk.BOTH, expand=True)

    # Add a style to make the table clearer
    style = ttk.Style()
    style.configure("Treeview", rowheight=25)
    style.configure("Treeview.Heading", font=('Helvetica', 12, 'bold'))
    style.configure("Treeview", font=('Helvetica', 10))

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
    print(f"Columns in calibration_data: {calibration_data.columns}")

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

        print(f"Columns in element_data for {selected_element}: {element_data.columns}")

        peaks = element_data['wavelength'].unique()
        linearity_data = []

        for peak in peaks:
            peak_data = element_data[element_data['wavelength'] == peak]
            intensities = peak_data['relative_intensity']
            concentrations = peak_data['concentration']

            # Skip peaks with insufficient data points
            if len(concentrations.unique()) < 2:
                print(f"Skipping peak {peak} due to insufficient data points for regression")
                continue

            print(f"Peak Data for wavelength {peak}: {peak_data}")
            r2, model, prediction_summary, mean_intensities, std_intensities = calculate_linearity(intensities, concentrations)
            linearity_data.append([peak, r2])

        linearity_df = pd.DataFrame(linearity_data, columns=["wavelength", "r2"])

        linearity_window = Toplevel(app.root)
        linearity_window.title("Select Peak for Best Linearity")

        peaks_frame = ttk.Frame(linearity_window)
        peaks_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

        canvas = tk.Canvas(peaks_frame)
        canvas.grid(row=0, column=0, sticky="nsew")

        scrollbar_y = ttk.Scrollbar(peaks_frame, orient='vertical', command=canvas.yview)
        scrollbar_y.grid(row=0, column=1, sticky='ns')
        scrollbar_x = ttk.Scrollbar(peaks_frame, orient='horizontal', command=canvas.xview)
        scrollbar_x.grid(row=1, column=0, sticky='ew')

        canvas.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        inner_frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner_frame, anchor="nw")

        check_vars = []

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

                # Filter the element data to use only the selected peak
                peak_data = element_data[element_data['wavelength'] == selected_peak['wavelength']]
                results_table = calculate_concentrations(model, new_data, selected_peak)
                display_results(peak_data, selected_peak, new_data, results_table, model, selected_peak['r2'], prediction_summary, mean_intensities, std_intensities, selected_element)

            linearity_window.destroy()

        save_button = ttk.Button(linearity_window, text="Save", command=save_selected_peak)
        save_button.grid(row=2, column=0, columnspan=2, pady=10)

    proceed_button = ttk.Button(element_window, text="Proceed", command=proceed)
    proceed_button.grid(row=1, column=0, columnspan=2, pady=10)

    element_window.mainloop()
