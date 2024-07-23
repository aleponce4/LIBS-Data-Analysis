# libs_app.py - Contains the app class, which initializes the main window and customizes its appearance.
import os
import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
from ttkthemes import ThemedTk
import sv_ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from tkinter import StringVar
import platform
import sys
import functools
import matplotlib.lines as mlines


class App:
    # Initialize the main window
    def __init__(self):
        # Improve font antialiasing
        if platform.system() == 'Windows':
            from ctypes import windll # type: ignore
            windll.shcore.SetProcessDpiAwareness(1)

        #Fix duplicate graph space?
        self.current_graph_space = None

        self.theme_name = "sun-valley"  # Store the theme name as an instance variable
        self.root = ThemedTk(theme="sun-valley")
        sv_ttk.set_theme("light")
        self.root.title("LIBS Identification Software")
        self.root.geometry("1920x1800")
        self.root.minsize(width=1920, height=1080)
        self.root.state("zoomed")  

        # Initialize mode_var after self.root is created
        self.mode_var = tk.StringVar(value="Analysis")

        # Set the Icon
        #self.root.iconbitmap('Icons/main_icon.ico')
        icon_path = os.path.join(os.path.dirname(__file__), 'Icons', 'main_icon.ico')
        self.root.iconbitmap(icon_path)

        # Add this line to initialize peak_values
        self.peak_labels = []
        self.peak_values = []
        self.arrows = []
        self.line = None

        # Set default values for threshold_percent and round_off_error_tolerance
        self.current_threshold_percent = 10
        self.round_off_error_tolerance = 0

        # Add this line to initialize round_off_error_tolerance_var
        self.round_off_error_tolerance_var = tk.IntVar()

        # Set normalized flag
        self.normalized = False

        # Import label peak function
        from label_peaks import label_peaks

        # Intitilize empty element list
        self.element_df = None

        # Initialize peak_data for csv export
        self.peak_data = []


        # Create the graph space
        if self.mode_var.get() == "Analysis":
            from graph_space import create_graph_space
            self.current_graph_space, self.fig, self.ax = create_graph_space(self)
        else:
            from live_graph_space import create_live_graph_space
            self.current_graph_space, self.fig, self.ax = create_live_graph_space(self)

        # Create the menu bar
        from menu_functions import create_sidebar
        create_sidebar(self)

        # Initialize spectrometer attribute
        self.spectrometer = None

        # Initialize selected_element
        self.selected_element = StringVar()

        # Bind the closing event to the custom function
        self.root.protocol("WM_DELETE_WINDOW", functools.partial(self.on_closing))


    # Run the main loop
    def run(self):
        self.root.mainloop()

    # Add the on_closing method inside the App class
    def on_closing(self):
        # Add any resource cleanup code here, if needed

        # Close the tkinter window
        self.root.destroy()

        # Terminate the program
        sys.exit(0)


# Run the app
if __name__ == "__main__":
    app = App()
    app.run()
