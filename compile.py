import os
import subprocess
from datetime import datetime

# Define paths and options
main_script = os.path.abspath("main.py")
base_output_dir = os.path.abspath("Compiled version")  # Output to local folder
icon_path = os.path.abspath("Icons\\main_icon.ico")

# Create a new directory with a timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_dir = os.path.join(base_output_dir, f"compiled_{timestamp}")
os.makedirs(output_dir, exist_ok=True)

# Use PyInstaller from virtual environment if available
pyinstaller_path = "pyinstaller"
if os.path.exists("LIBS_venv\\Scripts\\pyinstaller.exe"):
    pyinstaller_path = "LIBS_venv\\Scripts\\pyinstaller.exe"

# Comprehensive hidden imports (based on systematic analysis)
hidden_imports = [
    # Standard library modules that are sometimes missing (fixes PyInstaller bootstrap)
    "ipaddress", "urllib.parse", "pathlib", "email.mime.text", "email.mime.multipart", 
    "email.mime.base", "html.parser", "http.client", "http.server",
    
    # Core detected modules from dependency analysis
    "PIL", "PIL._tkinter_finder", "markdown", "matplotlib", "matplotlib.backends.backend_tkagg", 
    "matplotlib.figure", "numpy", "numpy.core._methods", "numpy.lib.format", "pandas", 
    "pandas._libs.tslibs.base", "pandas._libs.tslibs.nattype", "pywt", "scipy", 
    "scipy.sparse.csgraph._validation", "scipy.special._ufuncs", "sklearn", 
    "sklearn.neighbors.typedefs", "sklearn.utils._cython_blas", "statsmodels", 
    "sv_ttk", "textalloc", "tkhtmlview", "ttkthemes", "ttkthemes.themed_style", 
    "ttkthemes.themed_tk",
    
    # Additional problematic imports often missed
    "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
    "matplotlib.backends._backend_tk", "matplotlib.backends.backend_pdf",
    "scipy.stats", "scipy.optimize", "scipy.interpolate",
    "sklearn.ensemble", "sklearn.tree", "sklearn.linear_model",
    "numpy.random", "numpy.linalg", "numpy.fft",
    "pandas.io.formats.style", "pandas.plotting",
    
    # Additional imports for robustness
    "pkg_resources.py2_warn", "pkg_resources", "openpyxl", "xlsxwriter", "certifi", "urllib3"
]

# Build the command for PyInstaller
command = [
    pyinstaller_path,
    "--onefile",
    "--windowed",  # Back to windowed mode for production
    f"--icon={icon_path}",
    f"--distpath={output_dir}",
    "--clean",  # Clean cache and temporary files
    "--noconfirm",  # Replace output directory without asking
    
    # Add data files - CSV files
    f"--add-data={os.path.abspath('element_database.csv')};.",
    f"--add-data={os.path.abspath('persistent_lines.csv')};.",
    f"--add-data={os.path.abspath('calibration_data_library.csv')};.",
    
    # Add ALL icon files individually (this was the missing piece!)
    f"--add-data={os.path.abspath('Icons/add_to_library_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/apply_library_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/clean_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/export_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/help_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/Import_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/main_icon.ico')};Icons",
    f"--add-data={os.path.abspath('Icons/main_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/plot_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/savedata_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/search_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/spectrum_icon.png')};Icons",
    f"--add-data={os.path.abspath('Icons/Onteko_Logo.jpg')};Icons",
    
    # Add directories
    f"--add-data={os.path.abspath('Help')};Help", 
    f"--add-data={os.path.abspath('images')};images",
    
    # Set name to LIBS
    "--name=LIBS",
    main_script
]

# Add all hidden imports
for hidden_import in hidden_imports:
    command.extend(["--hidden-import", hidden_import])

# Add additional PyInstaller options for stability
command.extend([
    "--collect-all", "ttkthemes",  # Collect all ttkthemes data
    "--collect-all", "sv_ttk",     # Collect all sv_ttk data
])

# Print the command to be executed (for debugging)
print("Running command:", " ".join(command))

try:
    # Run the command
    subprocess.run(command, check=True)
    print(f"Executable created in {output_dir}")
    
    # Auto-cleanup: Keep only the 2 most recent builds
    try:
        compiled_dirs = []
        for item in os.listdir(base_output_dir):
            if item.startswith("compiled_") and os.path.isdir(os.path.join(base_output_dir, item)):
                compiled_dirs.append(item)
        
        # Sort by creation time (newest first)
        compiled_dirs.sort(reverse=True)
        
        # Remove all but the 2 newest
        if len(compiled_dirs) > 2:
            for old_dir in compiled_dirs[2:]:
                old_path = os.path.join(base_output_dir, old_dir)
                print(f"Removing old build: {old_dir}")
                import shutil
                shutil.rmtree(old_path)
                
    except Exception as cleanup_error:
        print(f"Warning: Could not clean old builds: {cleanup_error}")
        
except subprocess.CalledProcessError as e:
    print(f"An error occurred: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

