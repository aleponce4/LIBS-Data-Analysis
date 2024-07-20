# menu_functions.py contains functions that are used to create the sidebar menu and the acquisition buttons in the sidebar.
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
import csv
from adjust_spectrum import adjust_spectrum as actual_adjust_spectrum
from adjust_plot import adjust_plot as actual_adjust_plot
from calibration_element import calibration_table_window
from calibration_curve import process_data



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

# Reset data function
def reset_data(app):
    app.data = pd.DataFrame()
    app.x_data = pd.Series()
    app.y_data = pd.Series()

# Import data function
# Import data function
def import_data(app):
    reset_data(app)
    filetypes = [("Text files", "*.txt"), ("All files", "*.*")]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes)

    if not file_paths:
        return None

    all_data = pd.DataFrame()
    replicate_data = pd.DataFrame()

    for i, path in enumerate(file_paths):
        #print(f"Processing file: {path}")  # Debugging statement
        with open(path, 'r') as file:
            lines = file.readlines()

        # Identify the start of the spectral data
        start_index = next((index for index, line in enumerate(lines) if ">>>>>Begin Spectral Data<<<<<" in line), None)

        if start_index is None:
            # Attempt to find the data start by looking for the first numeric data
            for idx, line in enumerate(lines):
                try:
                    float(line.split()[0])
                    start_index = idx
                    break
                except ValueError:
                    continue

        if start_index is None:
            #print(f"No spectral data found in file: {path}")  # Debugging statement
            continue  # Skip files that don't have the expected format

        # Read the data from the identified start index
        data = pd.read_csv(path, sep='\t', engine='python', header=None, decimal='.', skiprows=start_index + 1)

        # Ensure to pick only the first two columns
        data = data.iloc[:, :2]

        # Rename columns for the data
        data.columns = ['Wavelength', f'Intensity_{i + 1}']

        # Flatten the part of the spectrum associated with the red laser peak
        lower_bound = 653.616
        upper_bound = 656.055

        # Create a mask for the red laser peak range
        red_laser_mask = (data['Wavelength'] >= lower_bound) & (data['Wavelength'] <= upper_bound)

        # Set the intensity values within the red laser peak range to 0
        data.loc[red_laser_mask, f'Intensity_{i + 1}'] = 0

        # Append to the all_data DataFrame for averaging
        all_data = pd.concat([all_data, data], axis=0)

        # Store the intensity values as separate columns in replicate_data
        if replicate_data.empty:
            replicate_data = data.copy()
        else:
            replicate_data = pd.merge(replicate_data, data, on='Wavelength', how='outer')

        #print(f"Columns after importing file {i + 1}: {replicate_data.columns}")  # Debugging statement

    if not all_data.empty:
        averaged_data = all_data.groupby('Wavelength').mean().reset_index()
        app.x_data, app.y_data = averaged_data['Wavelength'], averaged_data.iloc[:, 1:].mean(axis=1)
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

        # Determine the title
        if len(file_paths) > 1:
            file_name = "Averaged Data"
        else:
            file_name = os.path.basename(file_paths[0])

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


# Function to add to training library
def add_to_training_library(app):
    # Function to handle selected element from the periodic table window
    def handle_selected_element(selected_element, concentration, units, matched_wavelengths):
        app.selected_element = selected_element
        app.concentration = concentration
        app.units = units
        app.matched_wavelengths = matched_wavelengths



        # No further actions needed after closing the periodic table window

    # Check if data is available in app.data
    if app.data.empty:
        messagebox.showerror("Error", "No data available. Please import data first.")
        return

    # Open the periodic table window
    calibration_table_window(app, handle_selected_element)


# Function to apply calibration curve
def apply_calibration_curve(app):
    # Check if data is imported
    if app.data.empty:
        messagebox.showerror("Error", "No data available. Please import data first.")
        return

    # Load available elements from calibration_data_library.csv
    try:
        calibration_df = pd.read_csv('calibration_data_library.csv')
        available_elements = calibration_df['element_symbol'].unique()
    except FileNotFoundError:
        messagebox.showerror("Error", "Calibration data library not found.")
        return

    # Create a new Toplevel window for element selection
    selection_window = tk.Toplevel(app.root)
    selection_window.title("Select Element for Calibration")
    selection_window.geometry("300x200")

    # Label for dropdown
    label = ttk.Label(selection_window, text="Select Element:")
    label.pack(pady=10)

    # Dropdown menu for available elements
    selected_element = tk.StringVar()
    dropdown = ttk.Combobox(selection_window, textvariable=selected_element, values=available_elements)
    dropdown.pack(pady=10)

    def on_next():
        element = selected_element.get()
        if not element:
            messagebox.showerror("Error", "Please select an element.")
            return
        
        # Proceed to next steps in calibration_curve.py
        # Ensure that the selected element and imported data are accessible
        selection_window.destroy()
        app.selected_element = element
        proceed_with_calibration(app)

    # Next button
    next_button = ttk.Button(selection_window, text="Next", command=on_next)
    next_button.pack(pady=20)

def proceed_with_calibration(app):
    from calibration_curve import process_calibration
    process_calibration(app)







    




   










    




  
    
   










