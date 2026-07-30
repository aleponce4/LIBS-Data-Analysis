import logging

import pandas as pd
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.ticker as ticker
import statsmodels.api as sm

from prolibspector.analysis.calibration_library import calibration_library_exists, load_calibration_library
from prolibspector.core.ui_scale import scaled_font, scaled_int, ui_scale_for_widget
from prolibspector.core.ui_theme import apply_canvas_theme, apply_matplotlib_theme, get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog

logger = logging.getLogger(__name__)


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
        
        logger.debug("First lines of file %s: %s", idx + 1, file_content.splitlines()[:5])

        # Detect decimal separator and delimiter
        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        if '\t' in file_content:
            delimiter = '\t'
        elif ',' in file_content:
            delimiter = ','
        else:
            delimiter = r"\s+"  # Handle whitespace-delimited data

        # Read the data into a DataFrame, skipping the first row if it contains metadata
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)

        # Handle potential extra columns
        if data.shape[1] > 2:
            logger.warning("More than 2 columns detected in %s; trimming to the first two.", path)
            data = data.iloc[:, :2]

        if data.shape[1] == 2:
            data.columns = ['wavelength', f'intensity_rep{idx+1}']
        else:
            raise ValueError(f"Expected 2 columns but got {data.shape[1]} in file: {path}")

        if all_data.empty:
            all_data = data
        else:
            all_data = pd.merge(all_data, data, on='wavelength', how='outer')

    if not all_data.empty:
        return all_data
    else:
        messagebox.showerror("Error", "No data imported.", parent=app.root)
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


def build_peak_linearity_models(element_data):
    """Build per-peak regression data for the peak selection dialog."""
    linearity_data = []
    peak_models = {}

    for peak in element_data['wavelength'].unique():
        peak_data = element_data[element_data['wavelength'] == peak]
        intensities = peak_data['relative_intensity']
        concentrations = peak_data['concentration']

        # Skip peaks with insufficient data points
        if len(concentrations.unique()) < 2:
            logger.debug("Skipping peak %s: insufficient data points for regression.", peak)
            continue

        logger.debug("Peak data for wavelength %s: %s", peak, peak_data)
        r2, model, prediction_summary, mean_intensities, std_intensities = calculate_linearity(
            intensities,
            concentrations,
        )
        linearity_data.append([peak, r2])
        peak_models[peak] = {
            "model": model,
            "prediction_summary": prediction_summary,
            "mean_intensities": mean_intensities,
            "std_intensities": std_intensities,
        }

    return pd.DataFrame(linearity_data, columns=["wavelength", "r2"]), peak_models



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
    tolerance = 0.2  # Define a tolerance for peak matching

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
def display_results(element_data, selected_peak, new_data, results_table, model, r2, prediction_summary, mean_intensities, std_intensities, selected_element, parent=None):
    results_window = create_dialog(parent, "Calibration Results", resizable=True)
    scale = ui_scale_for_widget(results_window)
    palette = get_palette()

    # Plot the calibration curve
    fig, ax = plt.subplots()
    fig.subplots_adjust(right=0.7)

    # Calibration data
    peak_data = element_data[element_data['wavelength'] == selected_peak['wavelength']]
    concentrations_calibration = peak_data['concentration']

    logger.debug("Mean intensities: %s; std intensities: %s", mean_intensities.flatten(), std_intensities)

    # Ensure that the correct values are used for plotting
    ax.scatter(
        mean_intensities.flatten(),
        concentrations_calibration.unique(),
        label='Calibration Data',
        alpha=0.6,
        color=palette["spectrum_line"],
    )
    ax.plot(
        mean_intensities.flatten(),
        model.predict(sm.add_constant(mean_intensities)),
        label='Linear Fit',
        color=palette["plate_arrow"],
    )

    # Plot confidence bands with transparency
    ax.fill_between(
        mean_intensities.flatten(),
        prediction_summary['mean_ci_lower'],
        prediction_summary['mean_ci_upper'],
        color=palette["well_selected"],
        alpha=0.45,
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
        logger.debug("Sample data point: intensity=%s concentration=%s", closest_intensity, concentration)
        if first_label:
            ax.scatter(closest_intensity, concentration, label='Samples', color=palette["text"], alpha=0.6)
            first_label = False
        else:
            ax.scatter(closest_intensity, concentration, color=palette["text"], alpha=0.6)

    apply_matplotlib_theme(fig, ax, None, palette)
    legend = ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))
    legend.get_frame().set_facecolor(palette["axes_bg"])
    legend.get_frame().set_edgecolor(palette["canvas_border"])
    for legend_text in legend.get_texts():
        legend_text.set_color(palette["text"])

    # Display the plot in the window. Detach from pyplot's global registry so
    # repeated calibrations don't accumulate figures (the Tk canvas keeps its
    # own reference).
    plt.close(fig)
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
            tree.tag_configure('summary', font=scaled_font(10, "bold", family="Helvetica", scale=scale))
            tree.item(tree.get_children()[-1], tags='summary')

    # Add lines to separate replicates from summary statistics
    tree.insert("", "end", values=("", ""))  # Add an empty row for separation

    # Pack treeview
    tree.pack(fill=tk.BOTH, expand=True)

    # Add a style to make the table clearer
    style = ttk.Style()
    style.configure("Treeview", rowheight=scaled_int(25, scale))
    style.configure("Treeview.Heading", font=scaled_font(12, "bold", family="Helvetica", scale=scale))
    style.configure("Treeview", font=scaled_font(10, family="Helvetica", scale=scale))
    apply_window_policy(
        results_window,
        parent=parent,
        preferred=(1200, 800),
        min_size=(760, 520),
        modal=False,
        resizable=True,
    )

# Main Calibration curve function
def apply_calibration_curve(app):
    # Import new data for calibration curve
    new_data = import_calibration_data(app)
    if new_data is None:
        return

    # Read the calibration data
    if not calibration_library_exists():
        messagebox.showerror("Error", "Calibration data library not found.", parent=app.root)
        return

    calibration_data = load_calibration_library()
    logger.debug("Columns in calibration_data: %s", list(calibration_data.columns))

    # Ask user to select an element
    element_window = create_dialog(app.root, "Select Element for Calibration", modal=True, resizable=True)

    ttk.Label(element_window, text="Select Element:").grid(row=0, column=0, padx=5, pady=5)
    element_var = tk.StringVar()
    element_dropdown = ttk.Combobox(element_window, textvariable=element_var, state="readonly")
    element_dropdown['values'] = sorted(set(calibration_data['element_symbol']))
    element_dropdown.grid(row=0, column=1, padx=5, pady=5)
    element_dropdown.focus_set()

    def proceed():
        selected_element = element_var.get()
        element_data = calibration_data[calibration_data['element_symbol'] == selected_element]

        logger.debug("Columns in element_data for %s: %s", selected_element, list(element_data.columns))

        linearity_df, peak_models = build_peak_linearity_models(element_data)
        if linearity_df.empty:
            messagebox.showerror(
                "Calibration Error",
                f"No usable calibration peaks found for {selected_element}.",
                parent=element_window,
            )
            return

        element_window.destroy()
        linearity_window = create_dialog(app.root, "Select Peak for Best Linearity", modal=True, resizable=True)
        scale = ui_scale_for_widget(linearity_window)
        linearity_window.rowconfigure(0, weight=1)
        linearity_window.columnconfigure(0, weight=1)

        peaks_frame = ttk.Frame(linearity_window)
        peaks_frame.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

        canvas = tk.Canvas(peaks_frame, highlightthickness=0)
        apply_canvas_theme(canvas)
        canvas.grid(row=0, column=0, sticky="nsew")

        scrollbar_y = ttk.Scrollbar(peaks_frame, orient='vertical', command=canvas.yview)
        scrollbar_y.grid(row=0, column=1, sticky='ns')
        scrollbar_x = ttk.Scrollbar(peaks_frame, orient='horizontal', command=canvas.xview)
        scrollbar_x.grid(row=1, column=0, sticky='ew')

        canvas.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        inner_frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner_frame, anchor="nw")

        check_vars = []

        ttk.Label(inner_frame, text="Select", font=scaled_font(10, "bold", family="Helvetica", scale=scale)).grid(row=0, column=0, padx=5, pady=5)
        ttk.Label(inner_frame, text="Wavelength (nm)", font=scaled_font(10, "bold", family="Helvetica", scale=scale)).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(inner_frame, text="Linearity (R²)", font=scaled_font(10, "bold", family="Helvetica", scale=scale)).grid(row=0, column=2, padx=(20, 5), pady=5)

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
                messagebox.showinfo(
                    "Selected Peak",
                    f"Selected peak: {selected_peak['wavelength']} nm with R²: {selected_peak['r2']:.4f}",
                    parent=linearity_window,
                )

                # Filter the element data to use only the selected peak
                peak_data = element_data[element_data['wavelength'] == selected_peak['wavelength']]
                selected_model_data = peak_models.get(selected_peak['wavelength'])
                if selected_model_data is None:
                    messagebox.showerror(
                        "Calibration Error",
                        "Could not find regression data for the selected peak.",
                        parent=linearity_window,
                    )
                    return

                selected_model = selected_model_data["model"]
                results_table = calculate_concentrations(selected_model, new_data, selected_peak)
                display_results(
                    peak_data,
                    selected_peak,
                    new_data,
                    results_table,
                    selected_model,
                    selected_peak['r2'],
                    selected_model_data["prediction_summary"],
                    selected_model_data["mean_intensities"],
                    selected_model_data["std_intensities"],
                    selected_element,
                    parent=app.root,
                )
                if hasattr(app, "set_analysis_status"):
                    app.set_analysis_status(f"Applied calibration curve for {selected_element}.")

            linearity_window.destroy()

        linearity_buttons = ttk.Frame(linearity_window)
        linearity_buttons.grid(row=2, column=0, columnspan=2, pady=10)
        save_button = ttk.Button(linearity_buttons, text="Save", command=save_selected_peak)
        save_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(linearity_buttons, text="Cancel", command=linearity_window.destroy).grid(row=0, column=1)
        apply_window_policy(
            linearity_window,
            parent=app.root,
            preferred=(520, 520),
            min_size=(420, 320),
            modal=True,
            resizable=True,
        )

    proceed_button = ttk.Button(element_window, text="Proceed", command=proceed)
    proceed_button.grid(row=1, column=0, columnspan=2, pady=10)
    apply_window_policy(
        element_window,
        parent=app.root,
        preferred=(360, 140),
        min_size=(320, 120),
        modal=True,
        resizable=True,
    )
    app.root.wait_window(element_window)
