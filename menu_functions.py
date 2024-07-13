import os
import tkinter as tk
from tkinter import Toplevel, filedialog
from tkinter import ttk
from tkinter import messagebox
import pandas as pd
import functools
from PIL import Image, ImageTk
from search_element import search_element, periodic_table_window
from graph_space import update_title
import graph_space
import numpy as np
from adjust_spectrum import adjust_spectrum
from adjust_plot import adjust_plot
import numpy as np
from calibration_curve import apply_calibration_curve
import csv

from adjust_spectrum import adjust_spectrum as actual_adjust_spectrum
from adjust_plot import adjust_plot as actual_adjust_plot



# Function to create acquisition buttons in the sidebar
def create_acquisition_buttons(app):
    buttons_frame = ttk.LabelFrame(app.root.sidebar_frame)
    buttons_frame.grid(row=1, column=0, padx=20, pady=2)

    icon_size = (40, 40)
    placeholder_icon_path = "Icons/Import_icon.png"
    for i in range(5):
        icon_image = Image.open(placeholder_icon_path).resize(icon_size, Image.ANTIALIAS)
        icon_photo = ImageTk.PhotoImage(icon_image)
        btn = ttk.Button(buttons_frame, text=f"Placeholder Button {i+1}", image=icon_photo, compound='left', style="LeftAligned.TButton", width=20)
        btn.grid(row=i, column=0, padx=10, pady=15)
        btn.icon_photo = icon_photo
    return buttons_frame

# Function to create the sidebar
def create_sidebar(app):
    app.root.sidebar_frame = ttk.Frame(app.root, width=280)
    app.root.sidebar_frame.place(x=0, y=0, width=280, relheight=1)

    # Create a new frame to hold the buttons and labels
    buttons_frame = ttk.LabelFrame(app.root.sidebar_frame)
    buttons_frame.grid(row=1, column=0, padx=30, pady=2)

    style = ttk.Style()
    style.configure("Emphasized.TLabel", font=("lato", 16, "bold"), foreground="black")
    app.root.sidebar_label = ttk.Label(app.root.sidebar_frame, text="Menu", style="Emphasized.TLabel")
    app.root.sidebar_label.grid(row=0, column=0, padx=10, pady=(60, 2))

    style.configure("LeftAligned.TButton", anchor='w')

    icons = [
        ("Import Data", "Icons/Import_icon.png", functools.partial(import_data, app)),
        ("Adjust Spectrum", "Icons/spectrum_icon.png", functools.partial(adjust_spectrum, app, app.ax)),
        ("Adjust Plot", "Icons/plot_icon.png", functools.partial(adjust_plot, app, app.ax)),
        ("Search Element", "Icons/search_icon.png", functools.partial(periodic_table_window, app, app.ax)),
        ("Export Plot", "Icons/export_icon.png", functools.partial(export_plot, app, app.ax)),
        ("Export Data", "Icons/savedata_icon.png", functools.partial(export_data, app)),
        ("Add to Training Library", "Icons/add_to_library_icon.png", functools.partial(add_to_training_library, app)),  
        ("Apply Calibration Curve", "Icons/apply_library_icon.png", functools.partial(apply_calibration_curve, app)), 
        ("Clean Plot", "Icons/clean_icon.png", functools.partial(clean_plot, app)),
    ]


    icon_size = (40, 40)
    for i, (text, icon_path, command) in enumerate(icons):
        icon_image = Image.open(icon_path).resize(icon_size, Image.ANTIALIAS)
        icon_photo = ImageTk.PhotoImage(icon_image)
        btn = ttk.Button(buttons_frame, text=text, image=icon_photo, compound='left', command=command, style="LeftAligned.TButton", width=20)
        btn.grid(row=i, column=0, padx=10, pady=15)
        btn.icon_photo = icon_photo

    logo = Image.open("Icons/Onteko_Logo.JPG")
    original_width, original_height = 1063, 344
    max_width = 200
    new_height = int((max_width / original_width) * original_height)
    logo_resized = logo.resize((max_width, new_height), Image.ANTIALIAS)
    logo_image = ImageTk.PhotoImage(logo_resized)
    logo_frame = ttk.Frame(app.root.sidebar_frame)
    logo_frame.place(x=0, rely=1.0, y=-new_height, anchor='sw', height=new_height, width=200)
    logo_frame.grid(row=60, column=0, padx=(10, 1), pady=(140, 5))

# Function to reset data
def reset_data(app):
    app.data = pd.DataFrame()
    app.x_data = pd.Series()
    app.y_data = pd.Series()

# Function to import data from files
def import_data(app):
    reset_data(app)
    filetypes = [("Text files", "*.txt"), ("All files", "*.*")]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes)

    all_data = pd.DataFrame()
    replicate_data = pd.DataFrame()
    for i, path in enumerate(file_paths):
        with open(path, 'r') as file:
            file_content = file.read()

        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        delimiter = '\t' if '\t' in file_content else ','
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)
        
        # Append to the all_data DataFrame for averaging
        all_data = pd.concat([all_data, data], axis=0)
        
        # Store the intensity values as separate columns in replicate_data
        if replicate_data.empty:
            replicate_data = data.copy()
            replicate_data.columns = ['Wavelength', f'Intensity_{i+1}']
        else:
            temp_df = data.copy()
            temp_df.columns = ['Wavelength', f'Intensity_{i+1}']
            replicate_data = pd.merge(replicate_data, temp_df, on='Wavelength', how='outer')

    if not all_data.empty:
        averaged_data = all_data.groupby(0).mean().reset_index()
        app.x_data, app.y_data = averaged_data[0], averaged_data[1]
        app.data = averaged_data
        
        # Store the replicate data in app for other parts of the code
        app.replicate_data = replicate_data

        app.ax.clear()
        app.ax.plot(app.x_data, app.y_data)
        app.line = app.ax.lines[-1]
        app.ax.set_xlim([100, 1000])
        app.ax.set_xlabel("Wavelength (nm)")
        app.ax.set_ylabel("Relative Intensity")
        app.ax.grid(which='both', linestyle='--', linewidth=0.5)
        file_name = ", ".join(os.path.basename(path) for path in file_paths)
       
        update_title(app, file_name)
        app.canvas.draw()
    else:
        app.x_data, app.y_data = pd.Series(), pd.Series()
        app.ax.clear()
        app.canvas.draw()


# Function to clean the plot
def clean_plot(app):
    app.ax.cla()
    app.ax.set_xlim([100, 1000])
    app.ax.set_xlabel("Wavelength (nm)")
    app.ax.set_ylabel("Relative Intensity")
    app.ax.grid(which='both', linestyle='--', linewidth=0.5)
    app.canvas.draw()

# Function to adjust the spectrum
def adjust_spectrum(app, ax):
    actual_adjust_spectrum(app, ax)

# Function to adjust the plot
def adjust_plot(app, ax):
    actual_adjust_plot(app, ax)


# Function to export the plot
def export_plot(app, ax):
    def ask_save_file_and_dpi():
        dpi_window = tk.Toplevel(app.root)
        dpi_window.title("DPI Option")

        ttk.Label(dpi_window, text="Resolution (DPI):").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        dpi_var = tk.StringVar()
        dpi_var.set("300")
        dpi_entry = ttk.Entry(dpi_window, textvariable=dpi_var)
        dpi_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        def save_file():
            dpi = int(dpi_var.get())
            filetypes = [("PDF", "*.pdf"), ("PNG", "*.png"), ("TIFF", "*.tiff"), ("All Files", "*.*")]
            file_path = filedialog.asksaveasfilename(defaultextension='.pdf', filetypes=filetypes)

            if file_path:
                file_ext = os.path.splitext(file_path)[1]
                filetype = file_ext[1:]
                ax.figure.savefig(file_path, format=filetype, dpi=dpi)
                messagebox.showinfo("Export Successful", f"The plot was successfully exported as {file_ext.upper()}")
            dpi_window.destroy()

        save_button = ttk.Button(dpi_window, text="Save", command=save_file)
        save_button.grid(row=1, column=0, columnspan=2, pady=10)

    ask_save_file_and_dpi()

# Function to export the data
def export_data(app):
    filetypes = [("CSV files", "*.csv"), ("Excel files", "*.xlsx")]
    file_path = filedialog.asksaveasfilename(title="Select a file", filetypes=filetypes, defaultextension=".csv")

    if file_path:
        if not app.peak_data:
            messagebox.showinfo("No data", "No peak data to export.")
            return

        df = pd.DataFrame(app.peak_data, columns=["wavelength", "element_symbol", "ionization_level", "relative_intensity"])
        file_extension = os.path.splitext(file_path)[1]
        if file_extension == ".csv":
            df.to_csv(file_path, index=False)
        elif file_extension == ".xlsx":
            df.to_excel(file_path, index=False)
        messagebox.showinfo("Export Successful", f"The data was successfully exported to {file_path}")


########################################################################################################################

# Quantitative LIBS functions
# Function to calculate FWHM
def calculate_fwhm(spectrum_data, peak_wavelength, peak_intensity):
    closest_idx = (np.abs(spectrum_data['wavelength'] - peak_wavelength)).argmin()
    half_max = peak_intensity / 2

    left_idx = closest_idx
    while left_idx > 0 and spectrum_data['intensity'].iloc[left_idx] > half_max:
        left_idx -= 1

    right_idx = closest_idx
    while right_idx < len(spectrum_data) - 1 and spectrum_data['intensity'].iloc[right_idx] > half_max:
        right_idx += 1

    fwhm = spectrum_data['wavelength'].iloc[right_idx] - spectrum_data['wavelength'].iloc[left_idx]
    return fwhm

# Function to calculate SNR with dynamic peak width (FWHM)
def calculate_snr(spectrum_data, peak_wavelength, peak_intensity):
    fwhm = calculate_fwhm(spectrum_data, peak_wavelength, peak_intensity)
    half_fwhm = fwhm / 2

    noise_region = spectrum_data[
        ((spectrum_data['wavelength'] < peak_wavelength - half_fwhm) & (spectrum_data['wavelength'] > peak_wavelength - 2 * fwhm)) |
        ((spectrum_data['wavelength'] > peak_wavelength + half_fwhm) & (spectrum_data['wavelength'] < peak_wavelength + 2 * fwhm))
    ]
    
    noise_level = np.std(noise_region['intensity'])  # Estimate noise as the standard deviation of the noise region
    snr = peak_intensity / noise_level if noise_level > 0 else float('inf')
    return snr


# Add to library function
def add_to_training_library(app):
    if not hasattr(app, 'replicate_data') or app.replicate_data.empty:
        messagebox.showerror("Error", "No replicate data found.")
        return

    spectrum_data = app.replicate_data.copy()

    # Convert app.peak_data to DataFrame
    peak_data_df = pd.DataFrame(app.peak_data, columns=["wavelength", "element_symbol", "ionization_level", "relative_intensity"])

    # Function to find the closest wavelength
    def find_closest_wavelength(target_wavelength):
        return spectrum_data.iloc[(spectrum_data['Wavelength'] - target_wavelength).abs().argsort()[:1]].squeeze()

    # Calculate SNR for each replicate and average them
    def calculate_average_snr(row):
        replicate_columns = [col for col in spectrum_data.columns if col.startswith('Intensity')]
        
        snrs = []
        for col in replicate_columns:
            closest_wavelength_row = find_closest_wavelength(row['wavelength'])
            replicate_intensity = closest_wavelength_row[col]
            if pd.notna(replicate_intensity):
                snr = calculate_snr(
                    spectrum_data[['Wavelength', col]].rename(columns={'Wavelength': 'wavelength', col: 'intensity'}),
                    closest_wavelength_row['Wavelength'],
                    replicate_intensity
                )

                snrs.append(snr)
        avg_snr = np.mean(snrs) if snrs else 0

        return avg_snr

    peak_data_df['SNR'] = peak_data_df.apply(calculate_average_snr, axis=1)


    # Create a new window
    window = Toplevel(app.root)
    window.title("Add to Training Library")

    # Dropdown for selecting element
    ttk.Label(window, text="Select Element:").grid(row=0, column=0, padx=5, pady=5)
    element_var = tk.StringVar()
    element_dropdown = ttk.Combobox(window, textvariable=element_var)
    element_dropdown['values'] = sorted(set(peak_data_df['element_symbol']))
    element_dropdown.grid(row=0, column=1, padx=5, pady=5)

    # Frame for table and checkboxes
    peaks_frame = ttk.Frame(window)
    peaks_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

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

    # Add header labels with padding for SNR column
    ttk.Label(inner_frame, text="Select", font=('Helvetica', 10, 'bold')).grid(row=0, column=0, padx=5, pady=5)
    ttk.Label(inner_frame, text="Wavelength (nm)", font=('Helvetica', 10, 'bold')).grid(row=0, column=1, padx=5, pady=5)
    ttk.Label(inner_frame, text="Intensity", font=('Helvetica', 10, 'bold')).grid(row=0, column=2, padx=5, pady=5)
    ttk.Label(inner_frame, text="SNR", font=('Helvetica', 10, 'bold')).grid(row=0, column=3, padx=(20, 5), pady=5)

    # Function to update the table when an element is selected
    def update_peaks_table(*args):
        for widget in inner_frame.winfo_children()[4:]:  # Keep the header labels
            widget.destroy()
        check_vars.clear()

        selected_element = element_var.get()
        element_data = peak_data_df[peak_data_df['element_symbol'] == selected_element]

        for idx, row in element_data.iterrows():
            check_var = tk.BooleanVar()
            check_vars.append(check_var)
            ttk.Checkbutton(inner_frame, variable=check_var).grid(row=idx + 1, column=0, sticky="w")
            ttk.Label(inner_frame, text=row['wavelength']).grid(row=idx + 1, column=1)
            ttk.Label(inner_frame, text=f"{row['relative_intensity']:.1f}").grid(row=idx + 1, column=2)
            ttk.Label(inner_frame, text=f"{row['SNR']:.2f}").grid(row=idx + 1, column=3, padx=(20, 5))

        inner_frame.update_idletasks()
        canvas.config(scrollregion=canvas.bbox("all"))

    element_var.trace('w', update_peaks_table)

    # Input fields for concentration and units
    ttk.Label(window, text="Concentration:").grid(row=2, column=0, padx=5, pady=5)
    concentration_var = tk.StringVar()
    concentration_entry = ttk.Entry(window, textvariable=concentration_var)
    concentration_entry.grid(row=2, column=1, padx=5, pady=5)

    ttk.Label(window, text="Units:").grid(row=3, column=0, padx=5, pady=5)
    units_var = tk.StringVar()
    units_entry = ttk.Entry(window, textvariable=units_var)
    units_entry.grid(row=3, column=1, padx=5, pady=5)

    # Function to save selected data to CSV
    def save_to_library():
        selected_peaks = []
        selected_element = element_var.get()
        for i, check_var in enumerate(check_vars):
            if check_var.get():
                element_data = peak_data_df[peak_data_df['element_symbol'] == selected_element]
                for idx, row in element_data.iterrows():
                    selected_peaks.append([
                        row['wavelength'], selected_element, row['ionization_level'],
                        row['relative_intensity'], concentration_var.get(), units_var.get()
                    ])

        # Save each peak entry individually to the CSV
        library_file = "calibration_data_library.csv"
        with open(library_file, 'a', newline='') as csvfile:
            csv_writer = csv.writer(csvfile)
            # Write header only if the file does not exist or is empty
            if not os.path.exists(library_file) or os.path.getsize(library_file) == 0:
                csv_writer.writerow(["wavelength", "element_symbol", "ionization_level", "relative_intensity", "concentration", "units"])
            for peak in selected_peaks:
                csv_writer.writerow(peak)

        messagebox.showinfo("Success", "Data added to training library successfully.")
        window.destroy()



    # Save button
    save_button = ttk.Button(window, text="Save", command=save_to_library)
    save_button.grid(row=4, column=0, columnspan=2, pady=10)

    # Initialize the peaks table
    update_peaks_table()










    




   










    




  
    
   










