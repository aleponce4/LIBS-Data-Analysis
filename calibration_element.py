# calibration_element.py searches for peak_values for the calirbation curve and returns the peak_values for the selected element.
import os
import tkinter as tk
from tkinter import ttk
from ttkthemes import ThemedTk
import sv_ttk
from tkinter import Toplevel
import csv
from tkinter import messagebox
import label_peaks
from ttkthemes import ThemedTk, ThemedStyle
from label_peaks import label_peaks
import markdown
from tkhtmlview import HTMLLabel
import pandas as pd
import numpy as np
from scipy.sparse import diags, eye
import scipy.sparse.linalg as splinalg
from sklearn.preprocessing import MinMaxScaler
from scipy.signal import savgol_filter
from tkinter import messagebox

# ================================================================================================
# Colors for each group
colors = {
    "alkali_metals": "#244d57",
    "alkaline_earth_metals": "#622e39",
    "transition_metals": "#433c65",
    "post_transition_metals": "#2f4d47",
    "metalloids": "#523e1b",
    "nonmetals": "#2a4165",
    "halogens": "#287F6B",
    "noble_gases": "#623846",
    "lanthanides": "#004a77",
    "actinides": "#613b28",
    "unknown": "#46474c"
}

# Element format: (symbol, name, atomic_number, column, row, group)
ELEMENTS = [

#Nonmetals:
("H", "Hydrogen", 1, 0, 0, "nonmetals"), ("C", "Carbon", 6, 13, 1, "nonmetals"), ("N", "Nitrogen", 7, 14, 1, "nonmetals"), ("O", "Oxygen", 8, 15, 1, "nonmetals"), ("P", "Phosphorus", 15, 14, 2, "nonmetals"), ("S", "Sulfur", 16, 15, 2, "nonmetals"), ("Se", "Selenium", 34, 15, 3, "nonmetals"),
#Alkali metals:
("Li", "Lithium", 3, 0, 1, "alkali_metals"), ("Na", "Sodium", 11, 0, 2, "alkali_metals"), ("K", "Potassium", 19, 0, 3, "alkali_metals"), ("Rb", "Rubidium", 37, 0, 4, "alkali_metals"), ("Cs", "Cesium", 55, 0, 5, "alkali_metals"), ("Fr", "Francium", 87, 0, 6, "alkali_metals"),
#Alkaline earth metals:
("Be", "Beryllium", 4, 1, 1, "alkaline_earth_metals"), ("Mg", "Magnesium", 12, 1, 2, "alkaline_earth_metals"), ("Ca", "Calcium", 20, 1, 3, "alkaline_earth_metals"), ("Sr", "Strontium", 38, 1, 4, "alkaline_earth_metals"), ("Ba", "Barium", 56, 1, 5, "alkaline_earth_metals"), ("Ra", "Radium", 88, 1, 6, "alkaline_earth_metals"),
#Halogens:
("F", "Fluorine", 9, 16, 1, "halogens"), ("Cl", "Chlorine", 17, 16, 2, "halogens"), ("Br", "Bromine", 35, 16, 3, "halogens"), ("I", "Iodine", 53, 16, 4, "halogens"), ("At", "Astatine", 85, 16, 5, "halogens"),
#Noble gases:
("He", "Helium", 2, 17, 0, "noble_gases"), ("Ne", "Neon", 10, 17, 1, "noble_gases"), ("Ar", "Argon", 18, 17, 2, "noble_gases"), ("Kr", "Krypton", 36, 17, 3, "noble_gases"), ("Xe", "Xenon", 54, 17, 4, "noble_gases"), ("Rn", "Radon", 86, 17, 5, "noble_gases"), ("Og", "Oganesson", 118, 17, 6, "unknown"),
#Metals:
("Al", "Aluminium", 13, 12, 2, "post_transition_metals"), ("Ga", "Gallium", 31, 12, 3, "post_transition_metals"), ("In", "Indium", 49, 12, 4, "post_transition_metals"), ("Tl", "Thallium", 81, 12, 5, "post_transition_metals"), ("Sn", "Tin", 50, 13, 4, "post_transition_metals"), ("Pb", "Lead", 82, 13, 5, "post_transition_metals"), ("Bi", "Bismuth", 83, 14, 5, "post_transition_metals"),
#Metalloids:
("B", "Boron", 5, 12, 1, "metalloids"), ("Si", "Silicon", 14, 13, 2, "metalloids"), ("Ge", "Germanium", 32, 13, 3, "metalloids"), ("As", "Arsenic", 33, 14, 3, "metalloids"), ("Sb", "Antimony", 51, 14, 4, "metalloids"), ("Te", "Tellurium", 52, 15, 4, "metalloids"), ("Po", "Polonium", 84, 15, 5, "metalloids"),
#Transition metals:
("Sc", "Scandium", 21, 2, 3, "transition_metals"), ("Ti", "Titanium", 22, 3, 3, "transition_metals"), ("V", "Vanadium", 23, 4, 3, "transition_metals"), ("Cr", "Chromium", 24, 5, 3, "transition_metals"), ("Mn", "Manganese", 25, 6, 3, "transition_metals"), ("Fe", "Iron", 26, 7, 3, "transition_metals"), ("Co", "Cobalt", 27, 8, 3, "transition_metals"), ("Ni", "Nickel", 28, 9, 3, "transition_metals"), ("Cu", "Copper", 29, 10, 3, "transition_metals"), ("Zn", "Zinc", 30, 11, 3, "transition_metals"), ("Y", "Yttrium", 39, 2, 4, "transition_metals"), ("Zr", "Zirconium", 40, 3, 4, "transition_metals"), ("Nb", "Niobium", 41, 4, 4, "transition_metals"), ("Mo", "Molybdenum", 42, 5, 4, "transition_metals"), ("Tc", "Technetium", 43, 6, 4, "transition_metals"), ("Ru", "Ruthenium", 44, 7, 4, "transition_metals"), ("Rh", "Rhodium", 45, 8, 4, "transition_metals"), ("Pd", "Palladium", 46, 9, 4, "transition_metals"), ("Ag", "Silver", 47, 10, 4, "transition_metals"), ("Cd", "Cadmium", 48, 11, 4, "transition_metals"), ("Hf", "Hafnium", 72, 3, 5, "transition_metals"), ("Ta", "Tantalum", 73, 4, 5, "transition_metals"), ("W", "Tungsten", 74, 5, 5, "transition_metals"), ("Re", "Rhenium", 75, 6, 5, "transition_metals"), ("Os", "Osmium", 76, 7, 5, "transition_metals"), ("Ir", "Iridium", 77, 8, 5, "transition_metals"), ("Pt", "Platinum", 78, 9, 5, "transition_metals"), ("Au", "Gold", 79, 10, 5, "transition_metals"), ("Hg", "Mercury", 80, 11, 5, "transition_metals"), ("Rf", "Rutherfordium", 104, 3, 6, "transition_metals"), ("Db", "Dubnium", 105, 4, 6, "transition_metals"), ("Sg", "Seaborgium", 106, 5, 6, "transition_metals"), ("Bh", "Bohrium", 107, 6, 6, "transition_metals"), ("Hs", "Hassium", 108, 7, 6, "transition_metals"),
#Lanthanides:
("La", "Lanthanum", 57, 2, 5, "lanthanides"), ("Ce", "Cerium", 58, 3, 8, "lanthanides"), ("Pr", "Praseodymium", 59, 4, 8, "lanthanides"), ("Nd", "Neodymium", 60, 5, 8, "lanthanides"), ("Pm", "Promethium", 61, 6, 8, "lanthanides"), ("Sm", "Samarium", 62, 7, 8, "lanthanides"), ("Eu", "Europium", 63, 8, 8, "lanthanides"), ("Gd", "Gadolinium", 64, 9, 8, "lanthanides"), ("Tb", "Terbium", 65, 10, 8, "lanthanides"), ("Dy", "Dysprosium", 66, 11, 8, "lanthanides"), ("Ho", "Holmium", 67, 12, 8, "lanthanides"), ("Er", "Erbium", 68, 13, 8, "lanthanides"), ("Tm", "Thulium", 69, 14, 8, "lanthanides"), ("Yb", "Ytterbium", 70, 15, 8, "lanthanides"), ("Lu", "Lutetium", 71, 16, 8, "lanthanides"),
#Actinides:
("Ac", "Actinium", 89, 2, 6, "actinides"), ("Th", "Thorium", 90, 3, 9, "actinides"), ("Pa", "Protactinium", 91, 4, 9, "actinides"), ("U", "Uranium", 92, 5, 9, "actinides"), ("Np", "Neptunium", 93, 6, 9, "actinides"), ("Pu", "Plutonium", 94, 7, 9,"actinides"), ("Am", "Americium", 95, 8, 9, "actinides"), ("Cm", "Curium", 96, 9, 9, "actinides"), ("Bk", "Berkelium", 97, 10, 9, "actinides"), ("Cf", "Californium", 98, 11, 9, "actinides"), ("Es", "Einsteinium", 99, 12, 9, "actinides"), ("Fm", "Fermium", 100, 13, 9, "actinides"), ("Md", "Mendelevium", 101, 14, 9, "actinides"), ("No", "Nobelium", 102, 15, 9, "actinides"), ("Lr", "Lawrencium", 103, 16, 9, "actinides"),
#Unknown:
("Mt", "Meitnerium", 109, 8, 6, "unknown"), ("Ds", "Darmstadtium", 110, 9, 6, "unknown"), ("Rg", "Roentgenium", 111, 10, 6, "unknown"), ("Cn", "Copernicium", 112, 11, 6, "unknown"), ("Nh", "Nihonium", 113, 12, 6, "unknown"), ("Fl", "Flerovium", 114, 13, 6, "unknown"), ("Mc", "Moscovium", 115, 14, 6, "unknown"), ("Lv", "Livermorium", 116, 15, 6, "unknown"), ("Ts", "Tennessine", 117, 16, 6, "unknown"), ("Og", "Oganesson", 118, 17, 6, "unknown")]
                                                           
def calibration_table_window(app, callback):
    # Create a new Toplevel instance for the periodic table window
    periodic_window = Toplevel(app.root)
    periodic_window.title("Periodic Table")
    periodic_window.geometry("1200x800")  # Adjust the size as needed

    # Set the window icon
    selected_elements = app.selected_elements = []

    # Create an empty row and column to create space between first 3 columns and rest of table
    periodic_window.grid_rowconfigure(0, minsize=20)
    periodic_window.grid_columnconfigure(1, minsize=20)
    periodic_window.grid_columnconfigure(2, minsize=20)
    periodic_window.grid_columnconfigure(3, minsize=20)

    # Configure rows and columns to create space between main block and lanthanides/actinides
    periodic_window.grid_rowconfigure(7, minsize=20)
    periodic_window.grid_rowconfigure(9, minsize=20)
    periodic_window.grid_columnconfigure(2, minsize=20)
    periodic_window.grid_columnconfigure(10, minsize=20)

    # Create a label for each group
    def on_element_click(element):
        if element not in selected_elements:
            if len(selected_elements) < 20:
                selected_elements.append(element)
            else:
                messagebox.showinfo("Error", "You can only select up to 20 elements.")
        else:
            selected_elements.remove(element)

    # Modify the button style to have rounded corners
    rounded_button = {
        "relief": "flat",
        "highlightthickness": 0,
        "compound": "center",
        "bd": 0,
        "anchor": "center",
        "justify": "center"
    }

    ttk.Style().layout("Rounded.TButton", [
        ("Button.border", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [
                ("Button.label", {"sticky": "nswe"})
            ]})
        ]})
    ])

    # Custom button class for elements
    class ElementButton(tk.Canvas):
        def __init__(self, master, element, name, number, command, bg_color, width, height):
            super().__init__(master, width=width, height=height, bg=bg_color, highlightthickness=0)
            self.element = element
            self.name = name
            self.number = number
            self.command = command
            self.bg_color = bg_color

            # Create a text label for each element
            self.create_text(width / 2, height / 2 - 2, text=element, fill="white", font=("lato", 14))
            self.create_text(width / 2, height / 2 + 18, text=name, fill="white", font=("lato", 6))
            self.create_text(10, 5, text=str(number), fill="white", font=("lato", 8))
            self.bind("<Button-1>", self.on_click)

        # Change the background color of the button
        def on_click(self, event):
            if self.element not in selected_elements:
                self.configure(bg="#C2B2A6")
            else:
                self.configure(bg=self.bg_color)
            self.command(self.element)

    # Create styles for each group
    for group, color in colors.items():
        style_name = f"{group}.TButton"
        ttk.Style().configure(style_name, background=color, foreground="white", font=("lato", 12), layout="Rounded.TButton")  # Set font, text color and layout here

    # Create a button for each element
    for element, name, number, x, y, group in ELEMENTS:
        bg_color = colors[group]
        button_width = 60
        button_height = 60
        button = ElementButton(periodic_window, element, name, number, command=on_element_click, bg_color=bg_color, width=button_width, height=button_height)
        button.grid(row=y, column=x, padx=1, pady=1)  # Adjust padx and pady to reduce space between buttons

    # Create a separate frame at the bottom for concentration and units
    input_frame = ttk.Frame(periodic_window)
    input_frame.grid(row=12, column=0, columnspan=18, pady=20)  # Adjust row and columnspan as needed

    # Variables to store concentration and units
    concentration_var = tk.StringVar()
    units_var = tk.StringVar()

    # Function for baseline correction using asymmetric least squares smoothing with sparse matrices and regularization
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

    # Function for preprocessing a single spectrum
    def preprocess_spectrum(df, window_length=11, polyorder=2, baseline_lambda=1e5, baseline_p=0.01, baseline_niter=10, eps=1e-6):
        df_preprocessed = df.copy()
        for col in df.columns[1:]:
            y = df[col].values
            baseline = baseline_correction(y, lam=baseline_lambda, p=baseline_p, niter=baseline_niter, eps=eps)
            corrected = y - baseline
            smoothed = savgol_filter(corrected, window_length, polyorder)
            df_preprocessed[col] = smoothed
        return df_preprocessed

    # Function to normalize the data
    def normalize_data(df):
        df_normalized = df.copy()
        scaler = MinMaxScaler()
        for col in df.columns[1:]:
            df_normalized[col] = scaler.fit_transform(df[[col]])
        return df_normalized

    # Function to find the closest wavelength in the processed data
    def find_closest_wavelength(wavelength, data_wavelengths):
        index = np.abs(data_wavelengths - wavelength).argmin()
        return data_wavelengths[index]

    # Function to handle the submit button
    def on_submit():
        if not selected_elements:
            messagebox.showerror("Error", "Please select an element.")
            return
        if not concentration_var.get():
            messagebox.showerror("Error", "Please enter the concentration.")
            return
        if not units_var.get():
            messagebox.showerror("Error", "Please enter the units.")
            return

        selected_element = selected_elements[0]
        concentration = concentration_var.get()
        units = units_var.get()
        
        # Preprocess the data
        app.data = preprocess_spectrum(app.data)
        app.data = normalize_data(app.data)

        # Load the element database
        element_df = pd.read_csv('element_database.csv')
        element_df['Wavelength'] = pd.to_numeric(element_df['Wavelength'], errors='coerce')
        element_df['Ionization Level'] = pd.to_numeric(element_df['Ionization Level'], errors='coerce')  # Convert Ionization Level to numeric
        element_peaks = element_df[element_df['Symbol'] == selected_element]
        element_peaks = element_peaks[element_peaks['Ionization Level'] <= 3]  # Assuming ionization levels are 1, 2, or 3
        element_peaks_wavelengths = element_peaks['Wavelength'].dropna().values


        # Find closest matching wavelengths
        processed_wavelengths = app.data['Wavelength'].values
        matched_peaks = []
        for w in element_peaks_wavelengths:
            closest_wavelength = find_closest_wavelength(w, processed_wavelengths)
            ionization_level = element_peaks[element_peaks['Wavelength'] == w]['Ionization Level'].values[0]
            matched_peaks.append((closest_wavelength, ionization_level))

        # Prepare the data to save
        calibration_data = []
        for closest_wavelength, ionization_level in matched_peaks:
            for col in app.data.columns[1:]:
                intensity = app.data.loc[app.data['Wavelength'] == closest_wavelength, col].values[0]
                row = [closest_wavelength, selected_element, ionization_level, intensity, concentration, units]
                calibration_data.append(row)
        
        calibration_df = pd.DataFrame(calibration_data, columns=['wavelength', 'element_symbol', 'ionization_level', 'relative_intensity', 'concentration', 'units'])


        # Save the data to calibration_data_library.csv
        file_path = 'calibration_data_library.csv'
        if os.path.exists(file_path):
            calibration_df.to_csv(file_path, mode='a', header=False, index=False)
        else:
            calibration_df.to_csv(file_path, mode='w', header=True, index=False)

        # Callback with the selected element, concentration, units, and matched wavelengths
        callback(selected_element, concentration, units, [peak[0] for peak in matched_peaks])
            
        periodic_window.destroy()

    # Input fields for concentration and units
    ttk.Label(input_frame, text="Concentration:").grid(row=0, column=0, padx=5, pady=5)
    concentration_entry = ttk.Entry(input_frame, textvariable=concentration_var)
    concentration_entry.grid(row=0, column=1, padx=5, pady=5)

    ttk.Label(input_frame, text="Units:").grid(row=0, column=2, padx=5, pady=5)
    units_entry = ttk.Entry(input_frame, textvariable=units_var)
    units_entry.grid(row=0, column=3, padx=5, pady=5)

    submit_button = ttk.Button(input_frame, text="Submit", command=on_submit)
    submit_button.grid(row=1, column=0, columnspan=4, pady=10)

    periodic_window.mainloop()









