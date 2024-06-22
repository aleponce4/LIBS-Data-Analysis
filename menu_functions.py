# menu_functions.py - Contains all the functions related to the menu items, such as import_data, label_peaks, adjust_spectrum, adjust_plot, etc.

import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
from tkinter import Toplevel
from tkinter import filedialog
from tkinter import messagebox
import tkinter.font as font
import pandas as pd
import os
import functools
from search_element import search_element, periodic_table_window
from graph_space import update_title
import graph_space
import live_graph_space
from tkinter import PhotoImage
from PIL import ImageTk, Image
import numpy as np
import importlib


def create_acquisition_buttons(app):
    # This function creates the buttons for the "Acquisition" mode

    # Create a new frame to hold the buttons
    buttons_frame = ttk.LabelFrame(app.root.sidebar_frame)
    buttons_frame.grid(row=1, column=0, padx=20, pady=2)

    # Create placeholder buttons
    # (replace these with actual buttons later)
    icon_size = (40, 40)  # Define the desired icon size
    placeholder_icon_path = "Icons/Import_icon.png"  # Placeholder icon path (replace with actual icon path later)
    for i in range(5):
        # Resize the icon
        icon_image = Image.open(placeholder_icon_path).resize(icon_size, Image.ANTIALIAS)
        icon_photo = ImageTk.PhotoImage(icon_image)

        # Create the button with the icon and left-justified text
        btn = ttk.Button(buttons_frame, text=f"Placeholder Button {i+1}", image=icon_photo, compound='left', style="LeftAligned.TButton", width=20)
        btn.grid(row=i, column=0, padx=10, pady=15)
        btn.icon_photo = icon_photo  # Keep a reference to the icon to prevent garbage collection

    # return the frame containing the buttons
    return buttons_frame

#=======================================================================================================================
# Creates the tkk sidebar and adds it to the main window:
def create_sidebar(app):

    # Create the sidebar frame:
    app.root.sidebar_frame = ttk.Frame(app.root, width=190)
    app.root.sidebar_frame.place(x=0, y=0, relwidth=0.1, relheight=1)

    # Create a new frame to hold the buttons and labels
    buttons_frame = ttk.LabelFrame(app.root.sidebar_frame)
    buttons_frame.grid(row=1, column=0, padx=20, pady=2)

    # Create a new style for the emphasized label
    style = ttk.Style()
    style.configure("Emphasized.TLabel", font=("lato", 16, "bold"), foreground="black")

    # Create the sidebar label:
    app.root.sidebar_label = ttk.Label(app.root.sidebar_frame, text="Menu", style="Emphasized.TLabel")
    app.root.sidebar_label.grid(row=0, column=0, padx=10, pady=(60, 2))

    # search_element_handler, that takes the required argument selected_element from the app instance and calls search_element with it
    def search_element_handler(app):
        selected_element = app.selected_element.get()
        search_element(app, selected_element)

    # Create a new style for the left-aligned button
    style.configure("LeftAligned.TButton", anchor='w')

    # Create the icons and their respective functions (import data, plot element, etc.):
    icons = [
        ("Import Data", "Icons/Import_icon.png", functools.partial(import_data, app)),
        ("Adjust Spectrum", "Icons/spectrum_icon.png", functools.partial(adjust_spectrum, app, app.ax)),
        ("Adjust Plot", "Icons/plot_icon.png", functools.partial(adjust_plot, app, app.ax)),
        ("Search Element", "Icons/search_icon.png", functools.partial(periodic_table_window, app, app.ax)),
        ("Export Plot", "Icons/export_icon.png", functools.partial(export_plot, app, app.ax)),
        ("Export Data", "Icons/savedata_icon.png", functools.partial(export_data, app)),
        ("Clean Plot", "Icons/clean_icon.png", functools.partial(clean_plot, app)),
        
    ]

    icon_size = (40, 40)  # Define the desired icon size
  
    # separate function to handle the button click
    def button_click_handler(app, command):
        command(app)

    # Create the buttons for each icon:
    for i, (text, icon_path, command) in enumerate(icons):
        # Resize the icon
        icon_image = Image.open(icon_path).resize(icon_size, Image.ANTIALIAS)
        icon_photo = ImageTk.PhotoImage(icon_image)

        # Create the button with the icon and left-justified text
        btn = ttk.Button(buttons_frame, text=text, image=icon_photo, compound='left', command=command, style="LeftAligned.TButton", width=20)
        btn.grid(row=i, column=0, padx=10, pady=15)
        btn.icon_photo = icon_photo  # Keep a reference to the icon to prevent garbage collection

    # Load the logo:    
    logo = Image.open("Icons/Onteko_Logo.JPG")

    # Calculate the new dimensions while maintaining the aspect ratio
    original_width = 1063
    original_height = 344
    max_width = 200
    new_height = int((max_width / original_width) * original_height)

    # Resize the logo
    logo_resized = logo.resize((max_width, new_height), Image.ANTIALIAS)
    # Convert the resized logo to a PhotoImage
    logo_image = ImageTk.PhotoImage(logo_resized)

    # Create a separate frame for the logo
    logo_frame = ttk.Frame(app.root.sidebar_frame)
    logo_frame.place(x=0, rely=1.0, y=-new_height, anchor='sw', height=new_height, width=200)
    logo_frame.grid(row=60, column=0, padx=(10,1), pady=(140, 5))

    # Add a mode switch button
    mode_var = tk.StringVar(value="Analysis")
    mode_switch = ttk.Checkbutton(app.root.sidebar_frame, text="Switch to Acquisition", variable=mode_var, onvalue="Acquisition", offvalue="Analysis")
    mode_switch.grid(row=2, column=0, padx=10, pady=(60, 2))

    # Add connection status label
    connection_status_label = ttk.Label(app.root.sidebar_frame, text="Spectrometer: Not connected")
    connection_status_label.grid(row=3, column=0, padx=10, pady=(10, 2))

    app.current_buttons_frame = create_analysis_buttons(app)


##################################################################################
    def on_mode_change(*args):
        # When the mode is changed, remove the current set of buttons and add the new set
        app.current_buttons_frame.destroy()
        if mode_var.get() == "Analysis":
            app.current_buttons_frame = create_analysis_buttons(app)
            mode_switch.config(text="Switch to Acquisition")
        else:
            app.current_buttons_frame = create_acquisition_buttons(app)
            mode_switch.config(text="Switch to Analysis")

        # Grid the new button frame
        app.current_buttons_frame.grid(row=1, column=0, padx=20, pady=2)

        # Destroy the old graph space if it exists
        if app.current_graph_space is not None:
            app.current_graph_space.destroy()

        # Unload the current graph space module and load the new one
        if mode_var.get() == "Analysis":
            importlib.reload(graph_space)
            from graph_space import create_graph_space
            app.current_graph_space, app.fig, app.ax = create_graph_space(app)
        else:
            try:
                from acquisition_functions import initialize_spectrometer
                app.spectrometer = initialize_spectrometer(app)
            # If an exception occurs, an error message box is displayed and the connection status label is updated to "Not connected":
            except Exception as e:
                connection_status_label.config(text="Spectrometer: Not connected")
                messagebox.showerror("Error", f"Failed to initialize spectrometer: {str(e)}")
            else:
                if app.spectrometer is not None:
                    connection_status_label.config(text=f"Spectrometer: {app.spectrometer.model}")
                else:
                    connection_status_label.config(text="No spectrometer found")

            importlib.reload(live_graph_space)
            from live_graph_space import create_live_graph_space
            app.current_graph_space, app.fig, app.ax = create_live_graph_space(app, app.spectrometer)

        # Pack the new graph space
        app.current_graph_space.place(relx=0.1, rely=0, relwidth=0.9, relheight=1)


#################################################################################################################################
        

        # If the initialization is successful, the connection status label is updated to "Connected". 
        if mode_var.get() == "Acquisition":
            try:
                from acquisition_functions import initialize_spectrometer
                spectrometer = initialize_spectrometer(app)
            # If an exception occurs, an error message box is displayed and the connection status label is updated to "Not connected":
            except Exception as e:
                connection_status_label.config(text="Spectrometer: Not connected")
                messagebox.showerror("Error", f"Failed to initialize spectrometer: {str(e)}")
            else:
                if spectrometer is not None:
                    connection_status_label.config(text=f"Spectrometer: {spectrometer.model}")
                else:
                    connection_status_label.config(text="No spectrometer found")


    mode_var.trace("w", on_mode_change)

    # Create a label widget with the logo image
    logo_label = ttk.Label(logo_frame, image=logo_image)

    # Place the logo at the bottom corner of the sidebar using pack
    logo_label.pack(side='bottom', anchor='sw', padx=(0), pady=0)

    # Keep a reference to the image object to avoid garbage collection
    logo_label.image = logo_image


def create_analysis_buttons(app):
    # This function creates the buttons for the "Analysis" mode

    # Create a new frame to hold the buttons and labels
    buttons_frame = ttk.LabelFrame(app.root.sidebar_frame)
    buttons_frame.grid(row=1, column=0, padx=20, pady=2)

    # Create the icons and their respective functions (import data, plot element, etc.):
    icons = [
        ("Import Data", "Icons/Import_icon.png", functools.partial(import_data, app)),
        ("Adjust Spectrum", "Icons/spectrum_icon.png", functools.partial(adjust_spectrum, app, app.ax)),
        ("Adjust Plot", "Icons/plot_icon.png", functools.partial(adjust_plot, app, app.ax)),
        ("Search Element", "Icons/search_icon.png", functools.partial(periodic_table_window, app, app.ax)),
        ("Export Plot", "Icons/export_icon.png", functools.partial(export_plot, app, app.ax)),
        ("Export Data", "Icons/savedata_icon.png", functools.partial(export_data, app)),
        ("Clean Plot", "Icons/clean_icon.png", functools.partial(clean_plot, app)),
    ]

    icon_size = (40, 40)  # Define the desired icon size

    # Create the buttons for each icon:
    for i, (text, icon_path, command) in enumerate(icons):
        # Resize the icon
        icon_image = Image.open(icon_path).resize(icon_size, Image.ANTIALIAS)
        icon_photo = ImageTk.PhotoImage(icon_image)

        # Create the button with the icon and left-justified text
        btn = ttk.Button(buttons_frame, text=text, image=icon_photo, compound='left', command=command, style="LeftAligned.TButton", width=20)
        btn.grid(row=i, column=0, padx=10, pady=15)
        btn.icon_photo = icon_photo  # Keep a reference to the icon to prevent garbage collection

    # return the frame containing the buttons
    return buttons_frame


#=======================================================================================================================

# Define reset_data function:
def reset_data(app):
    app.data = pd.DataFrame()
    app.x_data = pd.Series()
    app.y_data = pd.Series()


# define import button function
def import_data(app):
    reset_data(app)  # Reset the data before importing a new file
    filetypes = [("Text files", "*.txt"), ("All files", "*.*")]
    file_path = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes)  # Allows multiple file selection

    # Initialize an empty DataFrame to store data from all files
    all_data = pd.DataFrame()

    for path in file_path:
        with open(path, 'r') as file:
            file_content = file.read()

        # Determine the decimal separator and delimiter
        decimal_separator = ',' if ',' in file_content and '.' not in file_content else '.'
        delimiter = '\t' if '\t' in file_content else ','

        # Read the CSV file with the correct delimiter and decimal separator
        data = pd.read_csv(path, sep=delimiter, engine='python', header=None, decimal=decimal_separator, skiprows=1)
        
        # Append the data from each file
        all_data = pd.concat([all_data, data], axis=0)

    # Group by the first column (wavelength), and calculate the mean of the second column (intensity)
    if not all_data.empty:
        averaged_data = all_data.groupby(0).mean().reset_index()
        x = averaged_data[0]
        y = averaged_data[1]
    else:
        x, y = pd.Series(), pd.Series()  # Handle the case where no files are selected

    # Update the x_data and y_data attributes in the App class
    app.x_data = x
    app.y_data = y
    app.data = averaged_data

    # Clear the current plot
    app.ax.clear()

    # Plot the new data
    app.ax.plot(x, y)
    app.line = app.ax.lines[-1]  # Store the line object in the app class

    # Reapply the grid and axis labels
    app.ax.set_xlim([100, 1000])
    app.ax.set_xlabel("Wavelength (nm)")
    app.ax.set_ylabel("Relative Intensity")
    app.ax.grid(which='both', linestyle='--', linewidth=0.5)

    # Extract the file name from the file path
    file_name = ", ".join(os.path.basename(path) for path in file_path)
    # Set the graph title based on the CSV file name    
    update_title(app, file_name)

    # Redraw the canvas
    app.canvas.draw()


#=======================================================================================================================
# Define the clean_plot function:
def clean_plot(app):
    app.ax.cla()
    app.ax.set_xlim([100, 1000])
    app.ax.set_xlabel("Wavelength (nm)")
    app.ax.set_ylabel("Relative Intensity")
    app.ax.grid(which='both', linestyle='--', linewidth=0.5)
    app.canvas.draw()

# Define the adjust_spectrum function:
def adjust_spectrum(app, ax):
    from tkinter import Toplevel
    from adjust_spectrum import adjust_spectrum
    adjust_spectrum(app, ax)

# Define the adjust_plot function:
def adjust_plot(app, ax):
    from adjust_plot import adjust_plot
    adjust_plot(app, ax)

# Define the export_plot function:
def export_plot(app, ax):
    def ask_save_file_and_dpi():
        # Create a Toplevel window for DPI input
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
                # Extract the file extension
                file_ext = os.path.splitext(file_path)[1]

                # Set the appropriate filetype
                filetype = file_ext[1:]

                # Save the plot with the selected filetype and dpi
                ax.figure.savefig(file_path, format=filetype, dpi=dpi)

                # Show a message box indicating that the export was successful
                messagebox.showinfo("Export Successful", f"The plot was successfully exported as {file_ext.upper()}")


            # Close the DPI input window
            dpi_window.destroy()

        # Create and place the save button
        save_button = ttk.Button(dpi_window, text="Save", command=save_file)
        save_button.grid(row=1, column=0, columnspan=2, pady=10)

    ask_save_file_and_dpi()



#=======================================================================================================================
def export_data(app):
    filetypes = [("CSV files", "*.csv"), ("Excel files", "*.xlsx")]
    file_path = filedialog.asksaveasfilename(title="Select a file", filetypes=filetypes, defaultextension=".csv")
    
    if file_path:
        # Convert the peak data to a pandas DataFrame
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






    




   










    




  
    
   










