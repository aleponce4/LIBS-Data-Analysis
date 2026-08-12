"""Window chrome for Acquisition Mode: root grid, plot area, plate overview.

PUBLIC-EDITION MODULE
=====================
This is a public-edition implementation written against the attributes and
methods ``acquisition/app.py`` binds through ``_bind_shell_aliases()``. It owns
only layout: the root window's sidebar/plot column split, the embedded
Matplotlib canvas, and the collapsible plate-overview strip with its
horizontally scrolling plate-history area.

The private edition's shell additionally hosts the automated-stage camera
preview pane and the multi-monitor operator-console layout. Neither is part of
the public edition, so this shell simply does not create them - it is a
complete, working shell for the manual and simulated acquisition views.

Layout contract (``app.py`` depends on all of it):

    root
     ├─ column 0, row 0 : sidebar (created later by create_acquisition_sidebar)
     └─ column 1, row 0 : graph_container   (grid)
                           ├─ row 0 : plot holder (pack-only, holds graph_frame)
                           └─ row 1 : plate_overview_frame (grid/grid_forget)
                                        └─ pack: plate_history_canvas + scrollbar
                                                  └─ plate_history_inner (canvas window)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from prolibspector.acquisition.graph import create_acquisition_graph
from prolibspector.core.ui_theme import apply_canvas_theme, apply_matplotlib_theme, get_palette

DEFAULT_WINDOW_SIZE = (1500, 950)
MIN_WINDOW_SIZE = (1180, 760)
PLATE_OVERVIEW_HEIGHT = 440
PLATE_HISTORY_HEIGHT = 300


class BaseAcquisitionShell:
    """Build and own the Acquisition Mode window chrome."""

    def __init__(self, *, root: tk.Misc, window_title: str, sidebar_width: int) -> None:
        self.root = root
        self.window_title = window_title
        self.sidebar_width = int(sidebar_width)
        self._close_handler: Callable[[], None] | None = None

        self._configure_root()
        self._build_plot_area()
        self._build_plate_overview()

    # ── Root window ──────────────────────────────────────────────────────

    def _configure_root(self) -> None:
        root = self.root
        try:
            root.title(self.window_title)
        except tk.TclError:  # pragma: no cover - non-toplevel parent
            pass

        # Column 0 is the sidebar; create_acquisition_sidebar() grids into it
        # with grid_propagate(False), so the width is pinned here.
        root.columnconfigure(0, weight=0, minsize=self.sidebar_width)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        if isinstance(root, (tk.Tk, tk.Toplevel)):
            width, height = DEFAULT_WINDOW_SIZE
            root.minsize(*MIN_WINDOW_SIZE)
            root.geometry(f"{width}x{height}")

    # ── Plot area ────────────────────────────────────────────────────────

    def _build_plot_area(self) -> None:
        palette = get_palette()

        self.graph_container = ttk.Frame(self.root)
        self.graph_container.grid(row=0, column=1, sticky="nsew")
        self.graph_container.columnconfigure(0, weight=1)
        self.graph_container.rowconfigure(0, weight=1)

        # create_acquisition_graph() packs into its parent, so the plot gets its
        # own pack-only holder: graph_container itself uses grid.
        self._plot_holder = tk.Frame(self.graph_container, bg=palette["canvas_bg"])
        self._plot_holder.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))

        self.graph_frame, self.fig, self.ax, self.canvas, self.live_line = create_acquisition_graph(
            self._plot_holder
        )

    # ── Plate overview ───────────────────────────────────────────────────

    def _build_plate_overview(self) -> None:
        palette = get_palette()

        self.plate_overview_frame = ttk.LabelFrame(
            self.graph_container,
            text="High-Throughput Plate",
            padding=(8, 6),
            height=PLATE_OVERVIEW_HEIGHT,
        )
        # Left un-gridded: app.py grids it on demand via _show_plate_overview().
        self.plate_overview_frame.grid_propagate(False)

        history_holder = ttk.Frame(self.plate_overview_frame)
        history_holder.pack(fill=tk.BOTH, expand=True)

        self.plate_history_canvas = tk.Canvas(
            history_holder,
            height=PLATE_HISTORY_HEIGHT,
            bg=palette["canvas_bg"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.plate_history_scrollbar = ttk.Scrollbar(
            history_holder,
            orient=tk.HORIZONTAL,
            command=self.plate_history_canvas.xview,
        )
        self.plate_history_canvas.configure(xscrollcommand=self.plate_history_scrollbar.set)
        self.plate_history_canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.plate_history_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

        self.plate_history_inner = ttk.Frame(self.plate_history_canvas)
        self.plate_history_window = self.plate_history_canvas.create_window(
            (0, 0), window=self.plate_history_inner, anchor="nw"
        )

        def _refresh_scrollregion(_event: tk.Event | None = None) -> None:
            self.plate_history_canvas.configure(scrollregion=self.plate_history_canvas.bbox("all"))

        def _match_inner_height(event: tk.Event) -> None:
            self.plate_history_canvas.itemconfigure(self.plate_history_window, height=event.height)

        self.plate_history_inner.bind("<Configure>", _refresh_scrollregion)
        self.plate_history_canvas.bind("<Configure>", _match_inner_height)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def set_close_handler(self, handler: Callable[[], None]) -> None:
        """Route the window-manager close button to ``handler``."""
        self._close_handler = handler
        try:
            self.root.protocol("WM_DELETE_WINDOW", handler)
        except (AttributeError, tk.TclError):  # pragma: no cover - non-toplevel parent
            pass

    def reveal_for_run(self) -> None:
        """Show the window just before entering the Tk main loop."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.update_idletasks()
        except (AttributeError, tk.TclError):  # pragma: no cover
            pass

    def apply_theme(self) -> None:
        """Re-apply the saved palette to the plot and plate canvases."""
        palette = get_palette()
        apply_matplotlib_theme(self.fig, self.ax, line=self.live_line, palette=palette)
        self.live_line._normal_color = palette["spectrum_line"]
        try:
            self._plot_holder.configure(bg=palette["canvas_bg"])
            self.graph_frame.configure(bg=palette["canvas_bg"])
            self.canvas.get_tk_widget().configure(bg=palette["canvas_bg"])
        except tk.TclError:  # pragma: no cover
            pass
        apply_canvas_theme(self.plate_history_canvas, palette=palette)
        self.canvas.draw_idle()


__all__ = ["BaseAcquisitionShell"]
