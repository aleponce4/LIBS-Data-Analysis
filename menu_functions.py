import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import functools
from PIL import Image, ImageTk
from search_element import search_element, periodic_table_window
from graph_space import update_title
import graph_space
import live_graph_space
import numpy as np
from adjust_spectrum import adjust_spectrum
from adjust_plot import adjust_plot

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
    app.root.sidebar_frame = ttk.Frame(app.root, width=190)
    app.root.sidebar_frame.place(x=0, y=0, relwidth=0.1, relheight=1)

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
    for path in file_paths:
        with open(path, 'r') as file:
            file_content = file.read()

        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        delimiter = '\t' if '\t' in file_content else ','
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)
        all_data = pd.concat([all_data, data], axis=0)

    if not all_data.empty:
        averaged_data = all_data.groupby(0).mean().reset_index()
        app.x_data, app.y_data = averaged_data[0], averaged_data[1]
        app.data = averaged_data
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
    adjust_spectrum(app, ax)

# Function to adjust the plot
def adjust_plot(app, ax):
    adjust_plot(app, ax)

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






    




   










    




  
    
   










