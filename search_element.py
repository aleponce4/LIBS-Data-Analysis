# search_element.py searches for peak_values in the database, and adds element names to peak_labels.
import tkinter as tk
from tkinter import ttk
from ttkthemes import ThemedTk
import sv_ttk
from tkinter import Toplevel
import csv
from tkinter import messagebox
import pandas as pd
import label_peaks
from ttkthemes import ThemedTk, ThemedStyle
from label_peaks import label_peaks
import markdown
from tkhtmlview import HTMLLabel

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
                                                           
# Create a new window
def periodic_table_window(app, ax):
    if not app.line:
        messagebox.showerror("Error", "Please import data before searching elements.")
        return

    # Create a new ThemedTk instance for the periodic table window
    periodic_window = Toplevel(app.root)
    periodic_window.title("Periodic Table")

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


# ================================================================================================

    # Create a function to search for elements and ionization levels
    def apply_and_search(selected_elements, ionization_levels):
        periodic_window.destroy()
        element_df = search_element(app, selected_elements, ionization_levels)

    # Add a variable for each ionization level checkbutton
    ionization_level_1 = tk.BooleanVar()
    ionization_level_2 = tk.BooleanVar()
    ionization_level_3 = tk.BooleanVar()

   # Create a frame for the buttons
    button_frame = ttk.LabelFrame(periodic_window)
    button_frame.grid(row=12, column=0, columnspan=30, pady=5)

    # Create a label for "Select Ionization Level" text
    ionization_label = tk.Label(button_frame, text="Select Ionization Level")
    ionization_label.grid(row=0, column=2, pady=(20, 0), padx=(10, 5), sticky="w")

    # Create checkbuttons for ionization levels 1, 2, and 3
    ionization_checkbutton_1 = ttk.Checkbutton(button_frame, text="1", variable=ionization_level_1, width=5, padding=5)
    ionization_checkbutton_1.grid(row=0, column=3, pady=(20, 0), sticky="w")
    ionization_checkbutton_2 = ttk.Checkbutton(button_frame, text="2", variable=ionization_level_2, width=5, padding=5)
    ionization_checkbutton_2.grid(row=0, column=4, pady=(20, 0), sticky="w")
    ionization_checkbutton_3 = ttk.Checkbutton(button_frame, text="3", variable=ionization_level_3, width=5, padding=5)
    ionization_checkbutton_3.grid(row=0, column=5, pady=(20, 0), sticky="w")

    # Add the Help button
    help_button = ttk.Button(button_frame, text="Help", command=open_help_document)
    help_button.grid(row=0, column=6, pady=(20, 0), padx=(10, 10), sticky="w")

    # Create the apply button
    apply_button = ttk.Button(button_frame, text="Apply", command=lambda: apply_and_search(selected_elements, [ionization_level_1.get(), ionization_level_2.get(), ionization_level_3.get()]), width=20)
    apply_button.grid(row=0, column=7, pady=(20, 0), padx=(10, 10), sticky="w")
    app.selected_element = selected_elements

    # Function to change the database based on the selection
    def change_database(event):
        element_df_path = "element_database.csv"  # Default value
        if database_var.get() == "Standard database":
            element_df_path = "element_database.csv"
        elif database_var.get() == "Persistent Lines database":
            element_df_path = "persistent_lines.csv"
        print(f"Currently using {element_df_path} as the database.")
        app.element_df_path = element_df_path

    # Create a label for "Select database" text
    database_label = tk.Label(button_frame, text="Select database")
    database_label.grid(row=0, column=0, pady=(20, 0), padx=(10, 5), sticky="w")

    # Create a Combobox for database selection
    database_var = tk.StringVar()
    database_combobox = ttk.Combobox(button_frame, textvariable=database_var, state="readonly", width=25)
    database_combobox["values"] = ("Standard database", "Persistent Lines database")
    database_combobox.grid(row=0, column=1, pady=(20, 0), padx=(5, 10), sticky="w")
    database_combobox.current(0)  # Set default value to "Standard database"
    database_combobox.bind("<<ComboboxSelected>>", change_database)

    # Set the default path to element_database.csv
    app.element_df_path = "element_database.csv"


# Search for the element in element_database.csv based on the symbol, looking for in the first column of the csv file. 
def search_element(app, selected_elements, ionization_levels):
    element_df = pd.read_csv(app.element_df_path)

    # Convert the 'Ionization Level' column to a numeric data type, replacing non-numeric values with NaN.
    element_df['Ionization Level'] = pd.to_numeric(element_df['Ionization Level'], errors='coerce')
    # Remove rows with NaN values in the 'Ionization Level' column.
    element_df = element_df.dropna(subset=['Ionization Level'])
    # Convert the 'Ionization Level' column to integers.
    element_df['Ionization Level'] = element_df['Ionization Level'].astype(int)

    # Filter the dataframe to only include rows where the symbol matches the selected element
    filtered_element_df = element_df[element_df['Symbol'].isin(selected_elements)]

    # Further filter the dataframe based on selected ionization levels
    levels_to_include = [i + 1 for i, level in enumerate(ionization_levels) if level]
    filtered_element_ionization_df = filtered_element_df[filtered_element_df['Ionization Level'].isin(levels_to_include)]

    
    # If the element is not found, display an error message
    if len(filtered_element_df) == 0:
        messagebox.showinfo("Error", "Element not found")
        return None

    # Store the filtered DataFrame in app.element_df
    app.element_df = filtered_element_ionization_df

    
    # Call the label_peaks function with the filtered DataFrame
    label_peaks(app, app.ax, app.element_df)
        

def open_help_document():
# Define your markdown text
    markdown_text = """
# Periodic Table Window - Help Section

This window allows you to interact with a visual representation of the periodic table to select elements for analysis in your LIBS data processing software. Please find explanations for the key features below:

Selectable Elements: Click on any element in the periodic table to select it for analysis. You can select up to 20 elements at a time to avoid overwhelming the program and plot with labels.

Database Selection: Choose from two databases using the dropdown menu:

1.  **Standard Database (NIST LIBS Database):** This comprehensive database is provided by the National Institute of Standards and Technology and contains atomic and ionic spectral line data, including wavelengths, energy levels, and transition probabilities.
2.  **Persistent Lines Database (USA Army's Foundational Research Laboratory):** This database focuses on persistent or long-lived spectral lines\*, which remain visible for extended periods after the laser pulse. These lines can improve element identification accuracy and consistency in various conditions.

### Persistent/Long-lived Lines**:**

Typically, the spectral lines appear immediately after the laser pulse and disappear soon after. However, some lines, known as persistent or long-lived lines, remain visible for a longer duration after the laser pulse. The longevity of these lines is due to the specific properties of the atomic transitions they represent.

Persistent lines offer several advantages in LIBS analysis. Firstly, they are less likely to be obscured by the noise and other effects that are prominent immediately after the laser pulse. Secondly, they can provide reliable data even in complex samples where other lines might be hidden by the spectral signatures of other elements or compounds. Lastly, their long visibility period allows more time for measurement, which can improve signal-to-noise ratios and overall accuracy of the measurement.

### Ionization Levels:

In atomic and molecular physics, ionization levels refer to the different energy levels that an electron can occupy in an atom or a molecule. When an atom is subjected to a high-energy laser pulse, as in LIBS, electrons are excited from their ground state (level 1) to higher energy states (levels 2, 3, etc.). When these excited electrons return to their lower energy states, they emit light at specific wavelengths, creating the spectral lines we see in LIBS.

By selecting to search only for peaks from ionization levels 1, 2, and 3, you are focusing on the most fundamental and easily identifiable transitions. These transitions are generally more robust and less likely to be affected by environmental factors, leading to more accurate and consistent results.

Focusing on these levels can also simplify your data. Higher ionization levels can produce a large number of spectral lines, some of which may overlap with lines from other elements. By limiting the ionization levels, you reduce the number of lines to consider, making it easier to identify and quantify the elements in your sample.  
"""

    # Convert the markdown to HTML
    html_text = markdown.markdown(markdown_text)

    # Create a new tkinter window
    help_window = tk.Toplevel()
    help_window.title("Help")

    # Set the window to open in fullscreen
    help_window.attributes('-fullscreen', True)

    help_window.bind("<Escape>", lambda event: help_window.attributes("-fullscreen", False))

    # Create an HTMLLabel to display the HTML
    html_label = HTMLLabel(help_window, html=html_text)
    html_label.pack(fill="both", expand=True)







