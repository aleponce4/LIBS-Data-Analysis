# pyinstaller --onefile --add-data "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/Icons;Icons" --add-data "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/Help;Help" --add-data "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/images;images" --add-data "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/element_database.csv;." --add-data "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/persistent_lines.csv;." --icon "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/Icons/main_icon.ico" --name "ProLIBSpector" --distpath "C:/Users/Alex/Desktop/LIBS_changes/Compiled_version" --workpath "C:/Users/Alex/Desktop/LIBS_changes/Compiled_version/build" --additional-hooks-dir "C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/hooks" C:/Users/Alex/Desktop/LIBS_changes/LIBS-Data-Analysis/main.py


#Explanation of the Command:
#--onefile: Creates a one-file bundled executable.
#--add-data: Includes additional files and directories. The format is source_path;destination_path.
#--icon: Specifies the icon file for the executable.
#--name: Specifies the name of the output executable.
#--distpath: Specifies the output directory for the final executable.
#--workpath: Specifies the directory for temporary build files.
#--additional-hooks-dir: Specifies the directory containing custom hook files.
#The last argument is the path to your main Python script.