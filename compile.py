"""PyInstaller packaging script for the private release build.

NOT a supported public build path. This script expects the maintainer's
release virtual environment (``LIBS_venv``) and bundles vendor components
that are not part of the public edition and not declared as dependencies:
seabreeze, libusb, ttkthemes and scikit-learn. Running it from a plain
``pip install -e .`` checkout will fail at ``find_release_libusb_dll()``.

The public edition is run from source (see the README quick start); there is
no published frozen-executable build.
"""

import os
import shutil
import subprocess
import tkinter
import sys
from datetime import datetime

# Define paths and options
main_script = os.path.abspath("main.py")
base_output_dir = os.path.abspath("Compiled version")  # Output to local folder
icon_path = os.path.abspath("Icons\\main_icon.ico")
display_name = "LIBS Software"
primary_zip_name = f"{display_name}.zip"

# Locate Tcl/Tk library directories so PyInstaller bundles init.tcl
_root = tkinter.Tk(); _root.withdraw()
_tcl_dir = _root.tk.eval("info library")   # e.g. .../tcl/tcl8.6
_tk_dir = os.path.join(os.path.dirname(_tcl_dir),
                       f"tk{_root.tk.eval('info patchlevel').rsplit('.', 1)[0]}")
_root.destroy()
tcl_library_path = os.path.normpath(_tcl_dir)
tk_library_path  = os.path.normpath(_tk_dir)

# Create a new directory with a timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_dir = os.path.join(base_output_dir, f"compiled_{timestamp}")
os.makedirs(output_dir, exist_ok=True)

# Also prepare a separate output folder for an onedir build
output_dir_onedir = os.path.join(base_output_dir, f"compiled_{timestamp}_dir")
os.makedirs(output_dir_onedir, exist_ok=True)

# Use the project release virtual environment.
python_path = os.path.join("LIBS_venv", "Scripts", "python.exe")


def find_release_libusb_dll() -> str:
    """Locate libusb-1.0.dll inside the release virtual environment."""
    probe_script = (
        "from pathlib import Path\n"
        "import libusb_package\n"
        "dll = Path(libusb_package.__file__).resolve().parent / 'libusb-1.0.dll'\n"
        "print(dll if dll.is_file() else '')\n"
    )

    try:
        result = subprocess.run(
            [python_path, "-c", probe_script],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "LIBS_venv Python was not found. Build releases from the configured release virtual environment."
        ) from exc

    dll_path = ""
    for line in reversed(result.stdout.splitlines()):
        candidate = line.strip()
        if candidate:
            dll_path = candidate
            break

    if result.returncode != 0 or not dll_path or not os.path.isfile(dll_path):
        fallback = os.path.abspath(
            os.path.join("LIBS_venv", "Lib", "site-packages", "libusb_package", "libusb-1.0.dll")
        )
        if os.path.isfile(fallback):
            return fallback

        stderr = result.stderr.strip()
        detail = f" ({stderr})" if stderr else ""
        raise FileNotFoundError(
            "Could not find libusb-1.0.dll in LIBS_venv. "
            "Install the release dependency with: "
            r"LIBS_venv\Scripts\python.exe -m pip install libusb-package"
            f"{detail}"
        )

    return os.path.abspath(dll_path)

# Comprehensive hidden imports (based on systematic analysis)
hidden_imports = [
    # Standard library modules that are sometimes missing (fixes PyInstaller bootstrap)
    "ipaddress", "urllib.parse", "pathlib", "email.mime.text", "email.mime.multipart", 
    "email.mime.base", "html.parser", "http.client", "http.server",
    
    # Core detected modules from dependency analysis
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageTk",
    "PIL._tkinter_finder", "markdown", "matplotlib", "matplotlib.backends.backend_tkagg",
    "matplotlib.figure", "numpy", "numpy.core._methods", "numpy.lib.format", "pandas", 
    "pandas._libs.tslibs.base", "pandas._libs.tslibs.nattype", "pywt", "scipy", 
    "scipy.sparse.csgraph._validation", "scipy.special._ufuncs", "sklearn",
    "sklearn.utils._cython_blas", "statsmodels",
    "sv_ttk", "textalloc", "tkhtmlview", "ttkthemes", "ttkthemes.themed_style", 
    "ttkthemes.themed_tk",
    
    # Additional problematic imports often missed
    "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
    "matplotlib.backends._backend_tk", "matplotlib.backends.backend_pdf",
    "sklearn.linear_model",
    "numpy.random", "numpy.linalg", "numpy.fft",
    
    # Additional imports for robustness
    "pkg_resources", "openpyxl", "certifi", "urllib3",

    # Acquisition mode modules
    "seabreeze", "seabreeze.spectrometers", "usb", "usb.core", "usb.backend",
    "usb.backend.libusb1", "usb.backend.libusb0", "usb.backend.openusb",
    "libusb_package",
    "queue", "threading",
]

libusb_dll_path = find_release_libusb_dll()

def build_command(*, mode: str, distpath: str, name: str) -> list[str]:
    """Build the PyInstaller command for the requested artifact mode."""
    command = [
        python_path,
        "-m",
        "PyInstaller",
        f"--{mode}",
        "--windowed",  # Back to windowed mode for production
        f"--icon={icon_path}",
        f"--distpath={distpath}",
        "--clean",  # Clean cache and temporary files
        "--noconfirm",  # Replace output directory without asking

        # Add data files - CSV files
        f"--add-data={os.path.abspath('element_database.csv')};.",
        f"--add-data={os.path.abspath('persistent_lines.csv')};.",
        f"--add-data={os.path.abspath('calibration_data_library.csv')};.",

        # Add whole asset directories. Listing icons one by one used to drift
        # from what is on disk whenever an icon was added or removed.
        f"--add-data={os.path.abspath('Icons')};Icons",
        f"--add-data={os.path.abspath('Help')};Help",

        # Add Tcl/Tk libraries so init.tcl is found at runtime
        f"--add-data={tcl_library_path};lib/tcl8.6",
        f"--add-data={tk_library_path};lib/tk8.6",
        f"--add-binary={libusb_dll_path};.",

        # Set artifact name
        f"--name={name}",
        main_script,
    ]

    # Add all hidden imports
    for hidden_import in hidden_imports:
        command.extend(["--hidden-import", hidden_import])

    # Add additional PyInstaller options for stability
    command.extend([
        "--collect-all", "ttkthemes",  # Collect all ttkthemes data
        "--collect-all", "sv_ttk",     # Collect all sv_ttk data
        "--collect-all", "seabreeze",  # Collect all seabreeze data
    ])

    if mode == "onedir":
        # Keep the launchable app obvious while tucking support files into a subfolder.
        command.extend(["--contents-directory", "_internal"])

    return command


def cleanup_old_builds():
    """Keep only the 2 most recent build directories."""
    compiled_dirs = []
    for item in os.listdir(base_output_dir):
        if item.startswith("compiled_") and os.path.isdir(os.path.join(base_output_dir, item)):
            compiled_dirs.append(item)

    compiled_dirs.sort(reverse=True)
    if len(compiled_dirs) > 2:
        for old_dir in compiled_dirs[2:]:
            old_path = os.path.join(base_output_dir, old_dir)
            print(f"Removing old build: {old_dir}")
            shutil.rmtree(old_path)


def zip_onedir_build():
    zip_path = os.path.join(output_dir_onedir, primary_zip_name)
    shutil.make_archive(zip_path.replace(".zip", ""), "zip", output_dir_onedir)
    print(f"Zipped onedir build to {zip_path}")


primary_command = build_command(mode="onedir", distpath=output_dir_onedir, name=display_name)
fallback_command = build_command(mode="onefile", distpath=output_dir, name=display_name)

# Print the command to be executed (for debugging)
print("Running primary onedir command:", " ".join(primary_command))

try:
    # Build the primary onedir artifact first for faster startup distribution.
    subprocess.run(primary_command, check=True)
    print(f"One-folder build created in {output_dir_onedir}")

    try:
        zip_onedir_build()
    except Exception as ze:
        print(f"Warning: Could not create zip of onedir build: {ze}")

    # Keep a onefile artifact as a fallback download.
    try:
        print("Running fallback onefile command:", " ".join(fallback_command))
        subprocess.run(fallback_command, check=True)
        print(f"Onefile build created in {output_dir}")
    except subprocess.CalledProcessError as e:
        print(f"Onefile build failed: {e}")
    except Exception as e:
        print(f"Unexpected error during onefile build: {e}")

    try:
        cleanup_old_builds()
    except Exception as cleanup_error:
        print(f"Warning: Could not clean old builds: {cleanup_error}")

except subprocess.CalledProcessError as e:
    print(f"An error occurred: {e}")
    sys.exit(1)
except Exception as e:
    print(f"An unexpected error occurred: {e}")
    sys.exit(1)

