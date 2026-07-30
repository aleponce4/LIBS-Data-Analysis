"""Presets management dialog for saving, loading, and resetting settings."""

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import numpy as np

from prolibspector.analysis.adjust_spectrum import (
    apply_baseline_removal,
    apply_laser_removal,
    apply_smoothing,
)
from prolibspector.core.paths import settings_dir
from prolibspector.core.settings import delete_settings, get_default_settings, load_settings, save_settings
from prolibspector.core.ui_scale import scaled_font, scaled_wrap, ui_scale_for_widget
from prolibspector.core.ui_theme import get_palette
from prolibspector.core.windowing import apply_window_policy, create_dialog


def open_presets_dialog(app):
    """Open the presets management dialog with settings preview"""
    
    presets_window = create_dialog(app.root, "Adjustment Presets", modal=True, resizable=True)
    scale = ui_scale_for_widget(presets_window)
    
    # Track loaded settings
    loaded_settings = [None]  # Use list to allow modification in nested function
    
    # Main container
    main_frame = ttk.Frame(presets_window)
    main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    # Left side - Controls
    left_frame = ttk.Frame(main_frame)
    left_frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0, 10))
    
    # Title
    title_label = ttk.Label(left_frame, text="Adjustment Presets", 
                            font=scaled_font(14, "bold", family="lato", scale=scale))
    title_label.pack(pady=15)
    
    # Description
    desc_label = ttk.Label(left_frame, 
                           text="Save and load your adjustment settings",
                           font=scaled_font(9, family="lato", scale=scale), wraplength=scaled_wrap(200, scale))
    desc_label.pack(pady=5, padx=10)
    
    # Button frame
    button_frame = ttk.LabelFrame(left_frame, text="Actions", padding=10)
    button_frame.pack(pady=15, padx=10, fill=tk.X)
    
    # Save Settings Button
    def on_save():
        """Save the current settings that were applied in adjust_spectrum and adjust_plot"""
        try:
            # Load existing settings (which were saved when user clicked Apply in those windows)
            current_settings = load_settings()
            if current_settings is None:
                # If no settings exist yet, use defaults
                current_settings = get_default_settings()
            
            # Ensure both sections exist
            if "adjust_spectrum" not in current_settings:
                current_settings["adjust_spectrum"] = {}
            if "adjust_plot" not in current_settings:
                current_settings["adjust_plot"] = {}
            
            # Save to file
            success, msg = save_settings(current_settings)
            if success:
                messagebox.showinfo("Settings Saved", 
                                  "Current adjustment settings have been saved to preset file.", parent=presets_window)
                # Update preview with what was saved
                update_preview(current_settings)
                loaded_settings[0] = current_settings
            else:
                messagebox.showerror("Error", msg, parent=presets_window)
        except Exception as e:
            messagebox.showerror("Error", f"Could not save settings: {str(e)}", parent=presets_window)
    
    save_btn = ttk.Button(button_frame, text="Save Current Settings", 
                         command=on_save, width=25)
    save_btn.pack(pady=8, fill=tk.X)
    
    # Load Settings Button
    def on_load():
        settings = load_settings()
        if settings is None:
            messagebox.showinfo("No Presets", 
                              "No saved presets found.", parent=presets_window)
            return
        
        # Update the preview display
        update_preview(settings)
        loaded_settings[0] = settings
        messagebox.showinfo("Settings Loaded", 
                          "Presets loaded. Click 'Apply' to use them.", parent=presets_window)
    
    load_btn = ttk.Button(button_frame, text="Load Saved Settings", 
                         command=on_load, width=25)
    load_btn.pack(pady=8, fill=tk.X)
    
    # Apply Loaded Settings Button
    def on_apply():
        """Apply the loaded settings to current spectrum"""
        if loaded_settings[0] is None:
            messagebox.showwarning("No Settings", 
                                 "Please load settings first.", parent=presets_window)
            return
        
        if app.line is None:
            messagebox.showwarning("No Data", 
                                 "Please import data first before applying presets.", parent=presets_window)
            return
        
        try:
            import matplotlib.colors as mcolors
            
            x_data = app.line.get_xdata()
            y_data = app.line.get_ydata().copy()
            current_data = y_data.copy()
            
            spectrum_settings = loaded_settings[0].get("adjust_spectrum", {})
            plot_settings = loaded_settings[0].get("adjust_plot", {})
            
            applied_items = []
            
            # ===== APPLY SPECTRUM SETTINGS =====
            
            # Apply smoothing if strength > 0
            if spectrum_settings.get("smoothing_strength", 0) > 0:
                method = spectrum_settings.get("smoothing_method", "Moving average")
                strength = spectrum_settings.get("smoothing_strength", 1)
                current_data = apply_smoothing(current_data, method, strength)
                applied_items.append(f"Smoothing ({method})")
            
            # Apply laser removal
            if spectrum_settings.get("laser_removal_enabled", False):
                center_wl = spectrum_settings.get("laser_wavelength", 532.63)
                width = spectrum_settings.get("laser_removal_width", 2.0)
                current_data = apply_laser_removal(current_data, x_data, center_wl, width)
                applied_items.append("Laser removal")
            
            # Apply baseline removal
            if spectrum_settings.get("baseline_removal_enabled", False):
                smoothness_str = spectrum_settings.get("baseline_smoothness", "Medium (1e4)")
                asymmetry_str = spectrum_settings.get("baseline_asymmetry", "Balanced (0.001)")
                
                try:
                    lam = float(smoothness_str.split("(")[1].rstrip(")"))
                except (IndexError, ValueError):
                    lam = 1e4
                
                try:
                    p = float(asymmetry_str.split("(")[1].rstrip(")"))
                except (IndexError, ValueError):
                    p = 0.001
                
                current_data = apply_baseline_removal(current_data, lam=lam, p=p, niter=10, clip=False)
                applied_items.append("Baseline removal")
            
            # Update spectrum data
            app.y_data = current_data
            app.line.set_ydata(current_data)
            
            # ===== APPLY PLOT SETTINGS =====
            
            if plot_settings:
                # Apply axis limits
                x_start = plot_settings.get("x_axis_start")
                x_end = plot_settings.get("x_axis_end")
                y_start = plot_settings.get("y_axis_start")
                y_end = plot_settings.get("y_axis_end")
                
                if x_start is not None and x_end is not None:
                    app.ax.set_xlim(x_start, x_end)
                
                if y_start is not None and y_end is not None:
                    normalize_method = plot_settings.get("normalize_method")
                    if normalize_method is None:
                        normalize_method = "Min-Max" if plot_settings.get("normalize_enabled", False) else "None"

                    # Check if normalization is enabled
                    if normalize_method == "None":
                        app.ax.set_ylim(y_start, y_end)
                
                # Apply line color
                line_color = plot_settings.get("line_color")
                if line_color:
                    app.line.set_color(line_color)
                
                # Apply background color
                bg_color = plot_settings.get("background_color")
                if bg_color:
                    app.ax.set_facecolor(mcolors.to_rgba(bg_color))
                
                # Apply line width
                line_width = plot_settings.get("line_width")
                if line_width is not None:
                    app.line.set_linewidth(line_width)
                
                # Apply normalization
                if normalize_method == "Min-Max":
                    min_val = np.min(current_data)
                    max_val = np.max(current_data)
                    if max_val > min_val:
                        normalized = (current_data - min_val) / (max_val - min_val)
                        app.y_data = normalized
                        app.line.set_ydata(normalized)
                        app.ax.set_ylim(0, 1)
                        applied_items.append("Normalization")
                elif normalize_method == "Total Intensity":
                    clipped = np.clip(current_data, 0, None)
                    total = np.sum(clipped)
                    if total > 0:
                        normalized = clipped / total
                        app.y_data = normalized
                        app.line.set_ydata(normalized)
                        app.ax.set_ylim(0, normalized.max() * 1.05)
                        applied_items.append("Normalization")
                
                applied_items.append("Plot adjustments")
            
            # Redraw the canvas
            app.ax.relim()
            app.ax.autoscale_view()
            app.canvas.draw()
            
            # Show confirmation message
            if applied_items:
                messagebox.showinfo("Settings Applied", 
                                  f"Applied: {', '.join(applied_items)}", parent=presets_window)
            else:
                messagebox.showinfo("No Adjustments", 
                                  "No adjustments were enabled in the preset.", parent=presets_window)
        except Exception as e:
            import traceback
            messagebox.showerror("Error", f"Could not apply settings: {str(e)}\n\n{traceback.format_exc()}", parent=presets_window)
    
    apply_btn = ttk.Button(button_frame, text="Apply Settings", 
                          command=on_apply, width=25)
    apply_btn.pack(pady=8, fill=tk.X)
    
    # Reset to Defaults Button
    def on_reset():
        confirm = messagebox.askyesno("Reset Settings", 
                                     "Are you sure you want to reset to default settings?\n"
                                     "This will delete your saved presets.", parent=presets_window)
        if confirm:
            success, msg = delete_settings()
            if success:
                messagebox.showinfo("Reset Successful", 
                                  "Settings reset to defaults.", parent=presets_window)
                update_preview(get_default_settings())
                loaded_settings[0] = None
            else:
                messagebox.showerror("Error", msg, parent=presets_window)
    
    reset_btn = ttk.Button(button_frame, text="Reset to Defaults", 
                          command=on_reset, width=25)
    reset_btn.pack(pady=8, fill=tk.X)
    
    # Close Button
    close_btn = ttk.Button(button_frame, text="Close", 
                          command=presets_window.destroy, width=25)
    close_btn.pack(pady=8, fill=tk.X)
    
    # Info text
    info_label = ttk.Label(left_frame,
                          text=f"Settings are stored in:\n{settings_dir() / 'settings.json'}",
                          font=scaled_font(8, family="lato", scale=scale),
                          style="Muted.TLabel", justify=tk.CENTER)
    info_label.pack(side=tk.BOTTOM, pady=10, padx=5)
    
    # Right side - Settings Preview
    right_frame = ttk.LabelFrame(main_frame, text="Current Settings Preview", padding=10)
    right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))
    
    # Create a scrollable text widget for displaying settings
    text_frame = ttk.Frame(right_frame)
    text_frame.pack(fill=tk.BOTH, expand=True)
    
    scrollbar = ttk.Scrollbar(text_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    
    settings_text = tk.Text(text_frame, height=25, width=40, 
                           yscrollcommand=scrollbar.set, wrap=tk.WORD,
                           font=scaled_font(9, family="Courier", scale=scale), bg="#f0f0f0")
    settings_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=settings_text.yview)
    
    # Make text read-only
    settings_text.config(state=tk.DISABLED)
    
    def update_preview(settings):
        """Update the settings preview display"""
        settings_text.config(state=tk.NORMAL)
        settings_text.delete(1.0, tk.END)
        
        if settings is None:
            settings_text.insert(tk.END, "No settings loaded.\n\n"
                               "Load or save presets\nto see them here.")
        else:
            # Format and display spectrum settings
            settings_text.insert(tk.END, "═══ SPECTRUM SETTINGS ═══\n", "header")
            
            spectrum = settings.get("adjust_spectrum", {})
            
            settings_text.insert(tk.END, "\nSmoothing:\n")
            settings_text.insert(tk.END, f"  Method: {spectrum.get('smoothing_method', 'N/A')}\n")
            settings_text.insert(tk.END, f"  Strength: {spectrum.get('smoothing_strength', 'N/A')}\n")
            
            settings_text.insert(tk.END, "\nLaser Removal:\n")
            settings_text.insert(tk.END, f"  Enabled: {spectrum.get('laser_removal_enabled', False)}\n")
            settings_text.insert(tk.END, f"  Wavelength: {spectrum.get('laser_wavelength', 'N/A')} nm\n")
            settings_text.insert(tk.END, f"  Width: ±{spectrum.get('laser_removal_width', 'N/A')} nm\n")
            
            settings_text.insert(tk.END, "\nBaseline Removal:\n")
            settings_text.insert(tk.END, f"  Enabled: {spectrum.get('baseline_removal_enabled', False)}\n")
            settings_text.insert(tk.END, f"  Smoothness: {spectrum.get('baseline_smoothness', 'N/A')}\n")
            settings_text.insert(tk.END, f"  Asymmetry: {spectrum.get('baseline_asymmetry', 'N/A')}\n")
            
            # Display plot settings if available
            plot = settings.get("adjust_plot", {})
            if plot:
                settings_text.insert(tk.END, "\n═══ PLOT SETTINGS ═══\n", "header")
                settings_text.insert(tk.END, "\nAxis Limits:\n")
                settings_text.insert(tk.END, f"  X-axis: {plot.get('x_axis_start', 'N/A')} to {plot.get('x_axis_end', 'N/A')}\n")
                settings_text.insert(tk.END, f"  Y-axis: {plot.get('y_axis_start', 'N/A')} to {plot.get('y_axis_end', 'N/A')}\n")
                
                settings_text.insert(tk.END, "\nVisual:\n")
                settings_text.insert(tk.END, f"  Line Color: {plot.get('line_color', 'N/A')}\n")
                settings_text.insert(tk.END, f"  Background Color: {plot.get('background_color', 'N/A')}\n")
                settings_text.insert(tk.END, f"  Line Width: {plot.get('line_width', 'N/A')}\n")
                normalize_method = plot.get("normalize_method")
                if normalize_method is None:
                    normalize_method = "Min-Max" if plot.get("normalize_enabled", False) else "None"
                settings_text.insert(tk.END, f"  Normalize: {normalize_method}\n")
        
        settings_text.config(state=tk.DISABLED)
    
    # Configure text tags
    settings_text.tag_config("header", font=scaled_font(10, "bold", family="Courier", scale=scale))
    
    # Load and display any existing settings on startup
    existing_settings = load_settings()
    if existing_settings:
        update_preview(existing_settings)
        loaded_settings[0] = existing_settings
    else:
        update_preview(None)
    apply_window_policy(
        presets_window,
        parent=app.root,
        preferred=(780, 620),
        min_size=(620, 460),
        modal=True,
        resizable=True,
    )
