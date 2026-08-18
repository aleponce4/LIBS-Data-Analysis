"""Advanced data import dialog with preview and replicate selection."""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from prolibspector.core.ui_scale import scaled_font, scaled_wrap, ui_scale_for_widget
from prolibspector.core.ui_theme import apply_canvas_theme, apply_matplotlib_theme, get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog


def open_import_dialog(app):
    """Open the advanced import dialog."""
    filetypes = [
        ("All supported", "*.txt *.csv *.tsv *.dat *.xlsx *.xls"),
        ("Text files", "*.txt *.dat *.tsv"),
        ("CSV files", "*.csv"),
        ("Excel files", "*.xlsx *.xls"),
        ("All files", "*.*"),
    ]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes, parent=app.root)
    if not file_paths:
        return

    dialog = ImportDialog(app, file_paths)
    dialog.run()


class ImportDialog:
    def __init__(self, app, file_paths):
        self.app = app
        self.file_paths = list(file_paths)
        self.parsed_files = []       # List of DataFrames (2-col: Wavelength, Intensity)
        self.include_vars = []       # BooleanVars for each replicate checkbox
        self.file_names = [os.path.basename(p) for p in self.file_paths]

        # Parse settings (defaults)
        self.delimiter_var = None
        self.decimal_var = None
        self.skip_rows_var = None
        self.parse_warning_var = None
        self._replicate_canvas = None

    def run(self):
        """Create and show the import dialog window."""
        self.win = create_dialog(self.app.root, "Import Data", resizable=True)
        scale = ui_scale_for_widget(self.win)

        # ── Top: Parse settings ──────────────────────────────────────
        settings_frame = ttk.LabelFrame(self.win, text="Parse Settings")
        settings_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        # Delimiter
        ttk.Label(settings_frame, text="Delimiter:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.delimiter_var = tk.StringVar(value="Auto")
        delimiter_combo = ttk.Combobox(settings_frame, textvariable=self.delimiter_var, state="readonly", width=12,
                                       values=["Auto", "Tab", "Comma", "Semicolon", "Space"])
        delimiter_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Decimal separator
        ttk.Label(settings_frame, text="Decimal:").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.decimal_var = tk.StringVar(value="Auto")
        decimal_combo = ttk.Combobox(settings_frame, textvariable=self.decimal_var, state="readonly", width=8,
                                     values=["Auto", ".", ","])
        decimal_combo.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        # Skip rows
        ttk.Label(settings_frame, text="Skip rows:").grid(row=0, column=4, padx=5, pady=5, sticky="e")
        self.skip_rows_var = tk.IntVar(value=1)
        skip_spin = ttk.Spinbox(settings_frame, from_=0, to=20, textvariable=self.skip_rows_var, width=5)
        skip_spin.grid(row=0, column=5, padx=5, pady=5, sticky="w")

        # Refresh button
        refresh_btn = ttk.Button(settings_frame, text="Refresh Preview", command=self._refresh)
        refresh_btn.grid(row=0, column=6, padx=15, pady=5)

        # File count label
        self.file_count_label = ttk.Label(settings_frame, text=f"{len(self.file_paths)} file(s) selected")
        self.file_count_label.grid(row=0, column=7, padx=15, pady=5)

        # ── Middle: Notebook with two tabs ───────────────────────────
        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Tab 1: Data Preview (raw table)
        self.preview_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.preview_tab, text="Data Preview")

        # Tab 2: Replicate Selection (plots + checkboxes)
        self.replicate_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.replicate_tab, text="Replicate Selection")

        # ── Bottom: Action buttons ───────────────────────────────────
        btn_frame = ttk.Frame(self.win)
        btn_frame.pack(fill=tk.X, padx=10, pady=(5, 10))

        self.parse_warning_var = tk.StringVar(value="")
        ttk.Label(
            btn_frame,
            textvariable=self.parse_warning_var,
            foreground="#B85C00",
            wraplength=scaled_wrap(520, scale),
        ).pack(side=tk.LEFT, padx=(5, 12), fill=tk.X, expand=True)

        ttk.Button(btn_frame, text="Select All", command=self._select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Deselect All", command=self._deselect_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self._close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Import Selected", command=self._apply_import).pack(side=tk.RIGHT, padx=5)

        # Initial parse
        self._refresh()
        apply_window_policy(
            self.win,
            parent=self.app.root,
            preferred=(1100, 850),
            min_size=(820, 560),
            modal=False,
            resizable=True,
            remember_key="import_data",
        )

    # ── Parsing ──────────────────────────────────────────────────────

    def _detect_delimiter(self, content):
        """Auto-detect delimiter from file content."""
        if '\t' in content:
            return '\t'
        # Count commas vs semicolons in first few lines
        lines = content.split('\n')[:5]
        comma_count = sum(line.count(',') for line in lines)
        semi_count = sum(line.count(';') for line in lines)
        if semi_count > comma_count:
            return ';'
        if comma_count > 0:
            return ','
        return r'\s+'  # whitespace

    def _detect_decimal(self, content):
        """Auto-detect decimal separator."""
        if ',' in content and '.' not in content:
            return ','
        return '.'

    def _get_delimiter(self, content):
        """Get delimiter based on user selection or auto-detect."""
        choice = self.delimiter_var.get()
        mapping = {"Tab": "\t", "Comma": ",", "Semicolon": ";", "Space": r"\s+"}
        if choice == "Auto":
            return self._detect_delimiter(content)
        return mapping.get(choice, r"\s+")

    def _get_decimal(self, content):
        """Get decimal separator based on user selection or auto-detect."""
        choice = self.decimal_var.get()
        if choice == "Auto":
            return self._detect_decimal(content)
        return choice

    def _parse_file(self, path):
        """Parse a single file and return a 2-column DataFrame (Wavelength, Intensity)."""
        ext = os.path.splitext(path)[1].lower()

        if ext in ('.xlsx', '.xls'):
            # Excel files
            df = pd.read_excel(path, header=None, skiprows=self.skip_rows_var.get())
        else:
            # Text-based files
            with open(path, 'r', errors='replace') as f:
                content = f.read(65536)

            delimiter = self._get_delimiter(content)
            decimal = self._get_decimal(content)
            skip = self.skip_rows_var.get()

            df = pd.read_csv(path, sep=delimiter, engine='python', header=None,
                             decimal=decimal, skiprows=skip, on_bad_lines='skip')

        # Take only first two columns
        if df.shape[1] < 2:
            raise ValueError(f"File has only {df.shape[1]} column(s), need at least 2")
        df = df.iloc[:, :2].copy()
        df.columns = ['Wavelength', 'Intensity']

        # Convert to numeric
        df['Wavelength'] = pd.to_numeric(df['Wavelength'], errors='coerce')
        df['Intensity'] = pd.to_numeric(df['Intensity'], errors='coerce')
        df.dropna(inplace=True)

        return df

    def _parse_all(self):
        """Parse all selected files."""
        self.parsed_files = []
        errors = []
        for path in self.file_paths:
            try:
                df = self._parse_file(path)
                self.parsed_files.append(df)
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
                self.parsed_files.append(None)

        if errors:
            visible_errors = "; ".join(errors[:3])
            extra = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
            if self.parse_warning_var is not None:
                self.parse_warning_var.set(f"Parse warnings: {visible_errors}{extra}")
        elif self.parse_warning_var is not None:
            self.parse_warning_var.set("")

    # ── Refresh ──────────────────────────────────────────────────────

    def _refresh(self):
        """Re-parse files and rebuild both tabs."""
        window = getattr(self, "win", None)
        try:
            if window is not None:
                window.config(cursor="watch")
                window.update_idletasks()
        except tk.TclError:
            pass
        try:
            self._parse_all()
            self._build_preview_tab()
            self._build_replicate_tab()
        finally:
            try:
                if window is not None:
                    window.config(cursor="")
            except tk.TclError:
                pass

    # ── Tab 1: Data Preview ──────────────────────────────────────────

    def _build_preview_tab(self):
        """Build the data preview table showing raw parsed values."""
        # Clear old content
        for widget in self.preview_tab.winfo_children():
            widget.destroy()

        if not self.parsed_files or all(f is None for f in self.parsed_files):
            ttk.Label(self.preview_tab, text="No data parsed. Adjust settings and click Refresh.",
                      font=scaled_font(11, widget=self.preview_tab)).pack(pady=30)
            return

        # Show first file's data as preview (with file selector)
        top = ttk.Frame(self.preview_tab)
        top.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(top, text="Preview file:").pack(side=tk.LEFT, padx=5)
        self.preview_file_var = tk.StringVar(value=self.file_names[0] if self.file_names else "")
        file_combo = ttk.Combobox(top, textvariable=self.preview_file_var, state="readonly",
                                  values=self.file_names, width=40)
        file_combo.pack(side=tk.LEFT, padx=5)
        file_combo.bind("<<ComboboxSelected>>", lambda e: self._update_preview_table())

        # Info label
        self.preview_info_label = ttk.Label(top, text="")
        self.preview_info_label.pack(side=tk.LEFT, padx=15)

        # Treeview for data
        tree_frame = ttk.Frame(self.preview_tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.preview_tree = ttk.Treeview(tree_frame, columns=("wavelength", "intensity"), show="headings", height=20)
        self.preview_tree.heading("wavelength", text="Wavelength (nm)")
        self.preview_tree.heading("intensity", text="Intensity")
        self.preview_tree.column("wavelength", width=200, anchor="center")
        self.preview_tree.column("intensity", width=200, anchor="center")

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.preview_tree.yview)
        self.preview_tree.configure(yscrollcommand=scrollbar.set)
        self.preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._update_preview_table()

    def _update_preview_table(self):
        """Update the treeview with the selected file's data."""
        self.preview_tree.delete(*self.preview_tree.get_children())

        idx = self.file_names.index(self.preview_file_var.get()) if self.preview_file_var.get() in self.file_names else 0
        df = self.parsed_files[idx] if idx < len(self.parsed_files) else None

        if df is None or df.empty:
            self.preview_info_label.config(text="Failed to parse this file")
            return

        self.preview_info_label.config(text=f"{len(df)} rows  |  Wavelength: {df['Wavelength'].min():.1f} – {df['Wavelength'].max():.1f} nm")

        # Show max 500 rows in preview (sample evenly for performance)
        display_df = df if len(df) <= 500 else df.iloc[::max(1, len(df)//500)]
        for _, row in display_df.iterrows():
            self.preview_tree.insert("", tk.END, values=(f"{row['Wavelength']:.4f}", f"{row['Intensity']:.4f}"))

    # ── Tab 2: Replicate Selection ───────────────────────────────────

    def _build_replicate_tab(self):
        """Build the replicate selection tab with mini plots and checkboxes."""
        for widget in self.replicate_tab.winfo_children():
            widget.destroy()

        valid_count = sum(1 for f in self.parsed_files if f is not None)
        if valid_count == 0:
            ttk.Label(self.replicate_tab, text="No valid files to display.",
                      font=scaled_font(11, widget=self.replicate_tab)).pack(pady=30)
            return

        # Scrollable canvas for replicate cards
        palette = get_palette()
        canvas = tk.Canvas(self.replicate_tab)
        apply_canvas_theme(canvas, palette)
        self._replicate_canvas = canvas
        scrollbar = ttk.Scrollbar(self.replicate_tab, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mousewheel scrolling, scoped to the pointer being over the
        # replicate list (bind_all leaked into other windows and could target
        # a destroyed canvas after a refresh).
        def _on_mousewheel(event):
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except tk.TclError:
                pass
            return "break"

        def _bind_wheel(_event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(_event):
            try:
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass

        canvas.bind("<Enter>", _bind_wheel, add="+")
        canvas.bind("<Leave>", _unbind_wheel, add="+")
        canvas.bind("<Destroy>", _unbind_wheel, add="+")

        self.include_vars = []

        # Layout: 3 columns of replicate cards
        cols = 3
        for i, (df, name) in enumerate(zip(self.parsed_files, self.file_names)):
            row_idx = i // cols
            col_idx = i % cols

            card = ttk.LabelFrame(scroll_frame, text="")
            card.grid(row=row_idx, column=col_idx, padx=8, pady=8, sticky="nsew")

            # Checkbox + filename
            var = tk.BooleanVar(value=(df is not None))
            self.include_vars.append(var)

            header_frame = ttk.Frame(card)
            header_frame.pack(fill=tk.X, padx=5, pady=(5, 0))

            cb = ttk.Checkbutton(header_frame, variable=var, text=name, style="Toggle.TButton" if False else "TCheckbutton")
            cb.pack(side=tk.LEFT)

            if df is not None:
                info_text = f"{len(df)} pts | {df['Wavelength'].min():.0f}–{df['Wavelength'].max():.0f} nm"
                ttk.Label(header_frame, text=info_text, style="Muted.TLabel").pack(side=tk.RIGHT, padx=5)

            # Mini plot
            if df is not None:
                fig = Figure(figsize=(3.2, 1.6), dpi=80)
                fig.subplots_adjust(left=0.12, right=0.96, top=0.92, bottom=0.2)
                ax = fig.add_subplot(111)
                line, = ax.plot(df['Wavelength'], df['Intensity'], linewidth=0.5, color=palette["spectrum_line"])
                ax.set_xlabel("nm", fontsize=7)
                ax.set_ylabel("I", fontsize=7)
                ax.tick_params(labelsize=6)
                apply_matplotlib_theme(fig, ax, line, palette)

                plot_canvas = FigureCanvasTkAgg(fig, master=card)
                plot_canvas.get_tk_widget().configure(bg=palette["canvas_bg"], highlightthickness=0)
                plot_canvas.draw()
                plot_canvas.get_tk_widget().pack(padx=5, pady=(2, 5))
            else:
                ttk.Label(card, text="⚠ Failed to parse", foreground="red").pack(pady=20)

        # Configure grid weights
        for c in range(cols):
            scroll_frame.grid_columnconfigure(c, weight=1)

        # Unbind mousewheel when dialog closes
        def _on_close():
            self._close()
        self.win.protocol("WM_DELETE_WINDOW", _on_close)

    def _close(self):
        if self._replicate_canvas is not None:
            try:
                self._replicate_canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
        if getattr(self, "win", None) is not None:
            self.win.destroy()
            self.win = None

    # ── Select / Deselect ────────────────────────────────────────────

    def _select_all(self):
        for var in self.include_vars:
            var.set(True)

    def _deselect_all(self):
        for var in self.include_vars:
            var.set(False)

    # ── Apply Import ─────────────────────────────────────────────────

    def _apply_import(self):
        """Merge selected replicates and send to app."""
        selected = [(df, name) for df, name, var in
                     zip(self.parsed_files, self.file_names, self.include_vars)
                     if var.get() and df is not None]

        if not selected:
            messagebox.showwarning("No Selection", "Please select at least one file to import.", parent=self.win)
            return

        all_data = pd.DataFrame()
        replicate_data = pd.DataFrame()

        for i, (df, name) in enumerate(selected):
            file_df = df.copy()
            file_df.columns = ['Wavelength', f'Intensity_{i+1}']

            # Vertical concat for averaging
            all_data = pd.concat([all_data, file_df], axis=0)

            # Horizontal merge for replicate storage
            if replicate_data.empty:
                replicate_data = file_df.copy()
            else:
                replicate_data = pd.merge(replicate_data, file_df, on='Wavelength', how='outer')

        # Average
        averaged_data = all_data.groupby('Wavelength').mean().reset_index()
        file_label = selected[0][1] if len(selected) == 1 else f"{len(selected)} files"
        x_data = averaged_data['Wavelength']
        y_data = averaged_data.iloc[:, 1:].mean(axis=1)
        self.app.set_loaded_spectrum(
            x_data,
            y_data,
            data=averaged_data,
            replicate_data=replicate_data,
            title=file_label,
            status_text=f"Imported {len(selected)} file(s), {len(x_data)} points.",
        )

        # Clean up and close
        self._close()
