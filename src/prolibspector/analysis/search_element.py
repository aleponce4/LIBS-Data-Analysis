"""Element-search helpers for matching detected peaks to database entries."""
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import markdown
from tkhtmlview import HTMLLabel
from prolibspector.core.tooltip import attach_tooltip
from prolibspector.core.ui_scale import ui_scale_for_widget
from prolibspector.core.windowing import apply_window_policy, create_dialog, make_markdown_help_window, make_scrollable_frame

from prolibspector.analysis.element_database import (
    DEFAULT_DATABASE_LABEL,
    database_option_names,
    filter_element_database,
    get_database_path,
)
from prolibspector.analysis.label_peaks import label_peaks

# ================================================================================================
# The table data, tiles, and grid layout are shared with the mapping
# element selector via element_table (re-exported for compatibility).
from prolibspector.analysis.element_table import (  # noqa: E402
    MAX_SELECTED_ELEMENTS,
    build_periodic_grid,
)


# Create a new window
def periodic_table_window(app, ax):
    if not app.line:
        messagebox.showerror("Error", "Please import data before searching elements.", parent=app.root)
        return

    periodic_window = create_dialog(app.root, "Periodic Table", resizable=True)
    scale = ui_scale_for_widget(periodic_window)
    periodic_window.rowconfigure(0, weight=1)
    periodic_window.columnconfigure(0, weight=1)
    table_scroll, table_frame = make_scrollable_frame(periodic_window, orient="both")
    table_scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    selected_elements = app.selected_elements = []

    def on_selection_refused(_element):
        messagebox.showinfo(
            "Selection Limit",
            f"You can only select up to {MAX_SELECTED_ELEMENTS} elements.",
            parent=periodic_window,
        )

    build_periodic_grid(
        table_frame,
        selected=selected_elements,
        scale=scale,
        on_refused=on_selection_refused,
    )

# ================================================================================================

    # Store reference so label_peaks Back button can reopen it
    app.periodic_window = periodic_window

    # Create a function to search for elements and ionization levels
    def apply_and_search(selected_elements, ionization_levels):
        if not selected_elements:
            messagebox.showinfo(
                "No Elements Selected",
                "Click one or more elements before applying.",
                parent=periodic_window,
            )
            return
        periodic_window.withdraw()  # Hide instead of destroy so we can come back
        element_df = search_element(app, selected_elements, ionization_levels)
        if element_df is None:
            # No matching lines: bring the periodic table back so the user
            # is not left with no window at all.
            periodic_window.deiconify()

    # Add a variable for each ionization level checkbutton
    ionization_level_1 = tk.BooleanVar()
    ionization_level_2 = tk.BooleanVar()
    ionization_level_3 = tk.BooleanVar()

   # Create a frame for the buttons
    button_frame = ttk.LabelFrame(periodic_window)
    button_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))

    # Create a label for "Select Ionization Level" text
    ionization_label = ttk.Label(button_frame, text="Select Ionization Level")
    ionization_label.grid(row=0, column=2, pady=(20, 0), padx=(10, 5), sticky="w")
    attach_tooltip(
        ionization_label,
        "Which ionization states to match: 1 = neutral atoms (I), "
        "2 = singly ionized (II), 3 = doubly ionized (III).",
    )

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
        element_df_path = get_database_path(database_var.get())
        app.element_df_path = element_df_path
        if hasattr(app, "set_analysis_status"):
            app.set_analysis_status(f"Element database: {database_var.get()}")

    # Create a label for "Select database" text
    database_label = ttk.Label(button_frame, text="Select database")
    database_label.grid(row=0, column=0, pady=(20, 0), padx=(10, 5), sticky="w")

    # Create a Combobox for database selection
    database_var = tk.StringVar()
    database_combobox = ttk.Combobox(button_frame, textvariable=database_var, state="readonly", width=25)
    database_combobox["values"] = database_option_names()
    database_combobox.grid(row=0, column=1, pady=(20, 0), padx=(5, 10), sticky="w")
    database_combobox.current(0)  # Set default value to "Standard database"
    database_combobox.bind("<<ComboboxSelected>>", change_database)

    # Set the default path to element_database.csv
    app.element_df_path = get_database_path(DEFAULT_DATABASE_LABEL)
    apply_window_policy(
        periodic_window,
        parent=app.root,
        preferred=(1220, 760),
        min_size=(760, 520),
        modal=False,
        resizable=True,
        remember_key="periodic_table",
    )


# Search for the element in element_database.csv based on the symbol, looking for in the first column of the csv file. 
def search_element(app, selected_elements, ionization_levels):
    filtered_element_ionization_df = filter_element_database(app.element_df_path, selected_elements, ionization_levels)

    
    # If the element is not found, display an error message
    if len(filtered_element_ionization_df) == 0:
        messagebox.showinfo("Element Not Found", "No matching lines were found for the selected element(s).", parent=app.root)
        return None

    # Store the filtered DataFrame in app.element_df (.copy() avoids SettingWithCopyWarning)
    app.element_df = filtered_element_ionization_df.copy()
    if hasattr(app, "set_analysis_status"):
        app.set_analysis_status(
            f"Matched {len(app.element_df)} spectral line(s) for {len(selected_elements)} selected element(s)."
        )

    
    # Call the label_peaks function with the filtered DataFrame
    label_peaks(app, app.ax, app.element_df)
    return app.element_df
        

def open_help_document():
# Define your markdown text
    markdown_text = """
# Periodic Table Window - Help Section

This window allows you to interact with a visual representation of the periodic table to select elements for analysis in your LIBS data processing software. Please find explanations for the key features below:

Selectable Elements: Click on any element in the periodic table to select it for analysis. You can select up to 20 elements at a time to avoid overwhelming the program and plot with labels.

Database Selection: Choose from two databases using the dropdown menu:

1.  **Standard Database (NIST LIBS Database):** This reference database is provided by the National Institute of Standards and Technology and contains atomic and ionic spectral line data, including wavelengths, energy levels, and transition probabilities.
2.  **Persistent Lines Database (USA Army's Foundational Research Laboratory):** This database focuses on persistent or long-lived spectral lines*, which remain visible for extended periods after the laser pulse. These lines can improve element identification accuracy and consistency in various conditions.

### Persistent/Long-lived Lines**:**

Typically, the spectral lines appear immediately after the laser pulse and disappear soon after. However, some lines, known as persistent or long-lived lines, remain visible for a longer duration after the laser pulse. The longevity of these lines is due to the specific properties of the atomic transitions they represent.

Persistent lines offer several advantages in LIBS analysis. Firstly, they are less likely to be obscured by the noise and other effects that are prominent immediately after the laser pulse. Secondly, they can provide reliable data even in complex samples where other lines might be hidden by the spectral signatures of other elements or compounds. Lastly, their long visibility period allows more time for measurement, which can improve signal-to-noise ratios and overall accuracy of the measurement.

### Ionization Levels:

In atomic and molecular physics, ionization levels refer to the different energy levels that an electron can occupy in an atom or a molecule. When an atom is subjected to a high-energy laser pulse, as in LIBS, electrons are excited from their ground state (level 1) to higher energy states (levels 2, 3, etc.). When these excited electrons return to their lower energy states, they emit light at specific wavelengths, creating the spectral lines we see in LIBS.

By selecting to search only for peaks from ionization levels 1, 2, and 3, you are focusing on the most fundamental and easily identifiable transitions. These transitions are generally more consistent and less likely to be affected by environmental factors, leading to clearer results.

Focusing on these levels can also simplify your data. Higher ionization levels can produce a large number of spectral lines, some of which may overlap with lines from other elements. By limiting the ionization levels, you reduce the number of lines to consider, making it easier to identify and quantify the elements in your sample.  
"""

    # Convert the markdown to HTML
    html_text = markdown.markdown(markdown_text)

    make_markdown_help_window(
        tk._default_root,
        "Periodic Table Help",
        lambda: html_text,
        lambda parent, html: HTMLLabel(parent, html=html),
    )







