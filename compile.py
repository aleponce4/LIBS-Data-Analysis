import os
import subprocess
from datetime import datetime

# Define paths and options
main_script = os.path.abspath("main.py")
base_output_dir = os.path.abspath("Compiled version")  # Output to local folder
icon_path = os.path.abspath("Icons\\main_icon.ico")
include_dirs = [
    os.path.abspath("Icons") + "=Icons",
    os.path.abspath("Help") + "=Help",
    os.path.abspath("images") + "=images"
]

# Create a new directory with a timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output_dir = os.path.join(base_output_dir, f"compiled_{timestamp}")
os.makedirs(output_dir, exist_ok=True)

# Build the command
command = [
    "nuitka",
    "--onefile",
    f"--windows-icon-from-ico={icon_path}",
    f"--output-dir={output_dir}",
    "--enable-plugin=tk-inter",
    "--include-package-data=ttkthemes"
]

# Add include directories to the command
for include in include_dirs:
    command.append(f"--include-data-dir={include}")

# Add the main script
command.append(main_script)

# Print the command to be executed (for debugging)
print("Running command:", " ".join(command))

try:
    # Run the command
    subprocess.run(command, check=True)
    print(f"Executable created in {output_dir}")
except subprocess.CalledProcessError as e:
    print(f"An error occurred: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")

