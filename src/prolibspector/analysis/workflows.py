import os
import tkinter as tk
from tkinter import filedialog
from tkinter import ttk
from tkinter import messagebox
from prolibspector.analysis.graph import update_title
from prolibspector.analysis.adjust_spectrum import adjust_spectrum as actual_adjust_spectrum
from prolibspector.analysis.adjust_plot import adjust_plot as actual_adjust_plot
from prolibspector.analysis.mapping_analysis import export_mapping_result, materialize_mapping_spectra_to_csv
from prolibspector.analysis.spectrum_io import (
    export_processed_spectrum,
    load_plaintext_spectra,
    reset_analysis_data,
    spectrum_title_from_paths,
)
from prolibspector.analysis.training_library_dialog import open_training_library_dialog
from prolibspector.core.settings import get_ui_settings
from prolibspector.core.windowing import apply_window_policy, create_dialog


def apply_calibration_curve_lazy(app):
    """Import the calibration workflow only when the user opens it."""
    from prolibspector.analysis.calibration_curve import apply_calibration_curve

    return apply_calibration_curve(app)


# Function to import data from files
def import_data(app):
    reset_analysis_data(app)
    filetypes = [("Text files", "*.txt"), ("All files", "*.*")]
    file_paths = filedialog.askopenfilenames(title="Select data files", filetypes=filetypes, parent=app.root)

    if not file_paths:
        return None

    averaged_data, replicate_data = load_plaintext_spectra(file_paths)
    if not averaged_data.empty:
        title = spectrum_title_from_paths(file_paths)
        x_data = averaged_data['Wavelength']
        y_data = averaged_data.iloc[:, 1:].mean(axis=1)
        app.set_loaded_spectrum(
            x_data,
            y_data,
            data=averaged_data,
            replicate_data=replicate_data,
            title=title,
            status_text=f"Imported {len(file_paths)} file(s), {len(x_data)} points.",
        )
    else:
        reset_analysis_data(app)
        app.clear_spectrum()




# Function to clean the plot
def clean_plot(app):
    app.clear_spectrum(confirm=get_ui_settings()["confirm_clean_reset"])

# Function to adjust the spectrum
def adjust_spectrum(app, ax):
    actual_adjust_spectrum(app, ax)

# Function to adjust the plot
def adjust_plot(app, ax):
    actual_adjust_plot(app, ax)


# Function to export the plot
def export_plot(app, ax):
    has_spectrum = hasattr(app, "has_spectrum") and app.has_spectrum()
    has_mapping = hasattr(app, "has_mapping_result") and app.has_mapping_result()
    if not has_spectrum and not has_mapping:
        messagebox.showinfo("No data", "No spectrum data to export. Import data first.", parent=app.root)
        return

    def ask_save_file_and_dpi():
        dpi_window = create_dialog(app.root, "DPI Option", modal=True, resizable=True)

        ttk.Label(dpi_window, text="Resolution (DPI):").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        dpi_var = tk.StringVar()
        dpi_var.set(str(get_ui_settings()["default_export_dpi"]))
        dpi_entry = ttk.Entry(dpi_window, textvariable=dpi_var)
        dpi_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        dpi_entry.focus_set()
        dpi_window.bind("<Return>", lambda _event: save_file())

        def save_file():
            try:
                dpi = int(dpi_var.get())
                if dpi <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(
                    "Invalid DPI",
                    "Enter a positive whole number for the export resolution.",
                    parent=dpi_window,
                )
                return

            filetypes = [("PDF", "*.pdf"), ("PNG", "*.png"), ("TIFF", "*.tiff"), ("All Files", "*.*")]
            file_path = filedialog.asksaveasfilename(
                defaultextension='.pdf',
                filetypes=filetypes,
                parent=dpi_window,
            )

            if file_path:
                file_ext = os.path.splitext(file_path)[1]
                filetype = file_ext[1:]
                try:
                    ax.figure.savefig(file_path, format=filetype, dpi=dpi)
                except Exception as exc:
                    messagebox.showerror("Export Failed", str(exc), parent=dpi_window)
                    return
                messagebox.showinfo(
                    "Export Successful",
                    f"The plot was successfully exported as {file_ext.upper()}",
                    parent=dpi_window,
                )
                if hasattr(app, "set_analysis_status"):
                    app.set_analysis_status(f"Exported plot: {os.path.basename(file_path)}")
            dpi_window.destroy()

        button_frame = ttk.Frame(dpi_window)
        button_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Button(button_frame, text="Cancel", command=dpi_window.destroy).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Save", command=save_file).pack(side=tk.RIGHT, padx=5)
        apply_window_policy(
            dpi_window,
            parent=app.root,
            preferred=(340, 140),
            min_size=(300, 130),
            modal=True,
            resizable=True,
        )

    ask_save_file_and_dpi()

# Function to export the data
def export_data(app):
    if hasattr(app, "analysis_workspace_var") and app.analysis_workspace_var.get() == "mapping":
        export_mapping_data(app)
        return

    # Check that there is at least spectrum data to export
    if not app.has_spectrum():
        messagebox.showinfo("No data", "No spectrum data to export. Import data first.", parent=app.root)
        return

    filetypes = [("CSV files", "*.csv"), ("Excel files", "*.xlsx")]
    file_path = filedialog.asksaveasfilename(
        title="Save processed spectrum",
        filetypes=filetypes,
        defaultextension=".csv",
        parent=app.root,
    )

    if file_path:
        try:
            written_paths = export_processed_spectrum(app.x_data, app.y_data, app.peak_data, file_path)
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc), parent=app.root)
            return
        saved_msg = f"Processed spectrum saved to:\n{written_paths[0]}"
        if len(written_paths) > 1:
            saved_msg += f"\n\nPeak data saved to:\n{written_paths[1]}"

        messagebox.showinfo("Export Successful", saved_msg, parent=app.root)
        app.set_analysis_status(f"Exported data: {os.path.basename(file_path)}")


def export_mapping_data(app):
    if not hasattr(app, "has_mapping_result") or not app.has_mapping_result():
        messagebox.showinfo("No map", "No mapping result to export. Build a map first.", parent=app.root)
        return

    filetypes = [("CSV files", "*.csv"), ("Excel files", "*.xlsx")]
    file_path = filedialog.asksaveasfilename(
        title="Save mapping data",
        filetypes=filetypes,
        defaultextension=".csv",
        parent=app.root,
    )
    if not file_path:
        return
    try:
        written_paths = export_mapping_result(app.mapping_result, file_path)
    except Exception as exc:
        messagebox.showerror("Export Failed", str(exc), parent=app.root)
        return

    saved_msg = "Mapping data saved to:\n" + "\n".join(str(path) for path in written_paths)
    messagebox.showinfo("Export Successful", saved_msg, parent=app.root)
    app.set_analysis_status(f"Exported mapping data: {os.path.basename(file_path)}")


def export_mapping_spectra_csvs(app):
    run_data = getattr(app, "mapping_run_data", None)
    if run_data is None:
        messagebox.showinfo("No mapping folder", "Load a mapping folder first.", parent=app.root)
        return
    output_dir = filedialog.askdirectory(title="Export mapping spectra CSVs to folder", parent=app.root)
    if not output_dir:
        return
    try:
        written_paths = materialize_mapping_spectra_to_csv(run_data, output_dir)
    except Exception as exc:
        messagebox.showerror("Export Failed", str(exc), parent=app.root)
        return
    skipped = max(0, int(getattr(run_data, "shots_total", 0)) - len(written_paths))
    message = f"Wrote {len(written_paths)} spectrum CSV file(s) to:\n{output_dir}"
    if skipped:
        message += f"\n\nSkipped {skipped} missing or unreadable spectrum row(s)."
    messagebox.showinfo("Export Successful", message, parent=app.root)
    app.set_analysis_status(f"Exported {len(written_paths)} mapping spectrum CSV file(s); skipped {skipped}.")


# Add to library function
def add_to_training_library(app):
    open_training_library_dialog(app)










    




   










    




  
    
   









