# live_graph_space.py - Contains the create_live_graph_space function for creating the graph space using Matplotlib.

import os
import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
import matplotlib
from matplotlib.backend_managers import ToolManager
import matplotlib.pyplot as plt
import matplotlib.backends.backend_tkagg as tkagg
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends._backend_tk import NavigationToolbar2Tk
import numpy as np



################################################################################################################
# Get Spectrometer Info


def acquire_spectrum(spectrometer):
    if spectrometer is not None:
        wavelengths = spectrometer.wavelengths()
        intensities = spectrometer.intensities()
        return wavelengths, intensities

################################################################################################################
# Create Live Graph Space


# Set the default font to DejaVu Sans
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['font.size'] = 12

# CustomToolbar class
class CustomToolbar(NavigationToolbar2Tk):
    toolitems = [t for t in NavigationToolbar2Tk.toolitems if t[0] != 'Save']

# Define rezize event handler
def on_resize(event, canvas):
    canvas.draw()

# Create the graph space
def create_live_graph_space(app, spectrometer):
    graph_frame = tk.Frame(app.root)
    graph_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=(100, 0))

    # Create the figure and axes
    fig, ax = plt.subplots(figsize=(14, 8))  # Increase the height of the graph space
    fig.subplots_adjust(left=0.2) # change the left margin to 0.2
    ax.set_xlim([100, 1000])
    ax.set_xlabel("Live Wavelength (nm)")
    ax.set_ylabel("Relative Intensity")
    ax.grid(which='both', linestyle='--', linewidth=0.5)

    # Create the canvas
    canvas = FigureCanvasTkAgg(fig, master=graph_frame)
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 10))
    app.canvas = canvas

    # Create the toolbar
    toolbar = CustomToolbar(canvas, graph_frame, pack_toolbar=False)  # Use CustomToolbar instead of NavigationToolbar2Tk
    toolbar.update()
    toolbar.pack(side=tk.TOP, anchor=tk.E, padx=(1, 40))
    # Create the tool manager
    canvas.mpl_connect('resize_event', lambda event: on_resize(event, canvas))


     # Add a line object to the axes for updating
    line, = ax.plot([], [], 'r-')  # 'r-' means red color, line plot

    def update_plot():
        # Acquire a spectrum
        wavelengths, intensities = acquire_spectrum(spectrometer)
        if wavelengths is not None and intensities is not None:
            line.set_data(wavelengths, intensities)
            ax.relim()  # Recalculate limits
            ax.autoscale_view(True, True, True)  # Autoscale the view
            canvas.draw()  # Redraw the canvas

        # Call this function again after 1000 ms (1 second)
        app.root.after(1000, update_plot)

    # Call the update function for the first time
    update_plot()

    return graph_frame, fig, ax

# Update the graph space
def update_title(app, file_name):
    file_name_without_extension, _ = os.path.splitext(file_name)
    title = file_name_without_extension
    app.ax.set_title(title)

