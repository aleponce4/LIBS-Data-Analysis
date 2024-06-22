# main.py - Contains the main application loop and imports necessary modules and files. This file is the entry point for the application. 
# It also sets the TCL_LIBRARY and SV_TTK_THEME environment variables when running as a compiled executable.

# Importing necessary modules
import sys
import os
import traceback

def log_error_to_file(error_message, file_name="error_log.txt"):
    with open(file_name, "a") as error_file:
        error_file.write(f"{error_message}\n")

def global_exception_handler(type, value, tb):
    error_message = "".join(traceback.format_exception(type, value, tb))
    log_error_to_file(error_message)
    print("An error occurred. Please check the error log file.")

sys.excepthook = global_exception_handler

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)

from libs_app import App

if __name__ == "__main__":
    my_app = App()
    my_app.run()

if getattr(sys, 'frozen', False):
    # Running as a compiled executable
    application_path = os.path.dirname(sys.executable)

    # Set the environment variable for the TCL_LIBRARY
    os.environ['TCL_LIBRARY'] = os.path.join(application_path, 'lib', 'tcl8.6')

    # Set the environment variable for the SV_TTK_THEME
    os.environ['SV_TTK_THEME'] = os.path.join(application_path, 'sv_ttk', 'theme')


