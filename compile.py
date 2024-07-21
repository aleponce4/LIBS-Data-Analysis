import os
import subprocess
import sys
from datetime import datetime
import shutil

# Clean the directory
build_dir = "C:\\Users\\Alex\\Desktop\\LIBS_changes\\Compiled version"
if os.path.exists(build_dir):
    shutil.rmtree(build_dir)
os.makedirs(build_dir)

# 
def main():
    # Define the base command to compile the application using Nuitka
    command = [
        sys.executable,  # Use the Python executable from the current environment
        "-m", "nuitka",
        "--onefile",
        "--windows-icon-from-ico=Icons/main_icon.ico",
        "--include-data-dir=Icons=Icons",
        "--include-data-dir=Help=Help",
        "--include-data-dir=images=images",
        "--include-package=ttkthemes",  # Ensure the ttkthemes package is included
        "--output-dir=C:\\Users\\Alex\\Desktop\\LIBS_changes\\Compiled version",
        "--enable-plugin=tk-inter",  # Enable Tkinter plugin
        "--output-filename=ProLIBSpector.exe",  # Specify the output executable name
        "main.py"
    ]

    # Add the correct path to the ttkthemes directory
    theme_dir = os.path.join(sys.prefix, 'Lib', 'site-packages', 'ttkthemes')
    if not os.path.exists(theme_dir):
        raise FileNotFoundError(f"The specified theme directory does not exist: {theme_dir}")

    # Add theme files to the command
    command.append(f"--include-data-dir={theme_dir}=ttkthemes")

    # Ensure the output directory exists
    output_dir = "C:\\Users\\Alex\\Desktop\\LIBS_changes\\Compiled version"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Print the command for debugging
    print("Running command:", " ".join(command))

    # Run the Nuitka command
    try:
        result = subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Compilation successful.")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print("An error occurred during compilation.")
        print(e.stdout)
        print(e.stderr)

if __name__ == "__main__":
    main()