"""Matplotlib graph-space helpers for Analysis mode."""

import os
import tkinter as tk
from tkinter import ttk  # ttk is a submodule of tkinter for themed widgets
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.backends._backend_tk import NavigationToolbar2Tk

from prolibspector.core.ui_scale import apply_matplotlib_text_scale
from prolibspector.core.ui_theme import apply_matplotlib_theme, get_palette


# Set the default font to DejaVu Sans
apply_matplotlib_text_scale()

# CustomToolbar class
class CustomToolbar(NavigationToolbar2Tk):
    # Save is handled by the app's own export; the Subplots dialog is useless
    # for maps and visually clashes with the themed UI.
    toolitems = [t for t in NavigationToolbar2Tk.toolitems if t[0] not in ("Save", "Subplots")]


def style_exists(name: str) -> bool:
    """True when the active ttk theme defines a layout for `name`."""
    try:
        return bool(ttk.Style().layout(name))
    except tk.TclError:
        return False


def style_plot_toolbar(toolbar, palette=None) -> None:
    """Blend the classic-Tk matplotlib toolbar into the themed UI."""
    colors = palette or get_palette()
    try:
        toolbar.configure(background=colors["canvas_bg"])
    except tk.TclError:
        return
    for child in toolbar.winfo_children():
        try:
            child.configure(
                background=colors["canvas_bg"],
                highlightthickness=0,
                borderwidth=0,
                relief="flat",
            )
        except tk.TclError:
            continue
        try:
            child.configure(activebackground=colors["canvas_bg"])
        except tk.TclError:
            pass
    message_label = getattr(toolbar, "_message_label", None)
    if message_label is not None:
        try:
            message_label.configure(background=colors["canvas_bg"], foreground=colors["text"])
        except tk.TclError:
            pass


# Define rezize event handler
def on_resize(event, canvas):
    canvas.draw()

# Create the graph space
def create_graph_space(app, parent=None):
    apply_matplotlib_text_scale()
    parent = parent or app.root
    palette = get_palette()
    graph_frame = tk.Frame(parent, bg=palette["canvas_bg"])
    graph_frame.grid(row=0, column=1, sticky="nsew")
    parent.grid_rowconfigure(0, weight=1)
    parent.grid_columnconfigure(1, weight=1)

    # Create the figure and axes
    fig, ax = plt.subplots(figsize=(14, 8))  # Increase the height of the graph space
    fig.subplots_adjust(left=0.1) # change the left margin to 0.2
    ax.set_xlim([100, 1000])
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Relative Intensity")
    apply_matplotlib_theme(fig, ax, palette=palette)

    # Canvas and the mapping right rail share a user-draggable split.
    body_paned = ttk.PanedWindow(graph_frame, orient="horizontal")
    body_paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 10))
    app.mapping_body_paned = body_paned

    canvas_frame = ttk.Frame(body_paned)
    body_paned.add(canvas_frame, weight=1)

    # Create the canvas
    canvas = FigureCanvasTkAgg(fig, master=canvas_frame)
    canvas.get_tk_widget().configure(bg=palette["canvas_bg"], highlightthickness=0)
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    app.canvas = canvas

    card_style = "Card.TFrame" if style_exists("Card.TFrame") else "TFrame"
    detail_frame = ttk.Frame(body_paned, width=440, style=card_style, padding=(10, 8))
    detail_frame.grid_propagate(False)
    detail_frame.rowconfigure(6, weight=1)
    detail_frame.columnconfigure(0, weight=1)

    app.mapping_hover_var = tk.StringVar(value="")
    ttk.Label(
        detail_frame,
        textvariable=app.mapping_hover_var,
        style="AnalysisStatus.TLabel",
        justify="left",
        wraplength=400,
    ).grid(row=0, column=0, sticky="ew", pady=(0, 6))

    app.mapping_legend_title_var = tk.StringVar(value="Legend")
    ttk.Label(detail_frame, textvariable=app.mapping_legend_title_var, style="Emphasized.TLabel").grid(
        row=1, column=0, sticky="w", pady=(0, 4)
    )
    legend_frame = ttk.Frame(detail_frame)
    legend_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
    legend_frame.columnconfigure(0, weight=1)
    app.mapping_legend_frame = legend_frame

    ttk.Separator(detail_frame, orient="horizontal").grid(row=3, column=0, sticky="ew", pady=(0, 8))
    ttk.Label(detail_frame, text="Mapping Evidence", style="Emphasized.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 6))

    # Mini average-spectrum inset for the element-detail view; hidden otherwise.
    spectrum_frame = ttk.Frame(detail_frame)
    spectrum_frame.grid(row=5, column=0, sticky="ew", pady=(0, 8))
    spectrum_fig = Figure(figsize=(4.0, 1.9), dpi=100)
    spectrum_ax = spectrum_fig.add_subplot(111)
    spectrum_canvas = FigureCanvasTkAgg(spectrum_fig, master=spectrum_frame)
    spectrum_canvas.get_tk_widget().configure(bg=palette["canvas_bg"], highlightthickness=0)
    spectrum_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
    app.mapping_spectrum_frame = spectrum_frame
    app.mapping_spectrum_fig = spectrum_fig
    app.mapping_spectrum_ax = spectrum_ax
    app.mapping_spectrum_canvas = spectrum_canvas
    spectrum_frame.grid_remove()

    table_frame = ttk.Frame(detail_frame)
    table_frame.grid(row=6, column=0, sticky="nsew")
    table_frame.rowconfigure(0, weight=1)
    table_frame.columnconfigure(0, weight=1)
    tree = ttk.Treeview(table_frame, show="headings", height=18)
    tree.grid(row=0, column=0, sticky="nsew")
    tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree_scroll.grid(row=0, column=1, sticky="ns")
    tree.configure(yscrollcommand=tree_scroll.set)
    app.mapping_right_rail = detail_frame
    app.mapping_evidence_panel = detail_frame
    app.mapping_evidence_tree = tree
    # The rail starts hidden; the app adds/forgets it on the paned window.

    # Create the toolbar
    toolbar = CustomToolbar(canvas, graph_frame, pack_toolbar=False)  # Use CustomToolbar instead of NavigationToolbar2Tk
    toolbar.update()
    toolbar.pack(side=tk.TOP, anchor=tk.E, padx=(1, 40))
    style_plot_toolbar(toolbar, palette)
    app.plot_toolbar = toolbar
    canvas.mpl_connect('resize_event', lambda event: on_resize(event, canvas))

    return graph_frame, fig, ax

# Update the graph space
def update_title(app, file_name):
    file_name_without_extension, _ = os.path.splitext(file_name)
    title = file_name_without_extension
    app.ax.set_title(title)
