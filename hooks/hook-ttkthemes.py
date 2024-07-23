# hook-ttkthemes.py
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Collect all data files and submodules from ttkthemes
datas = collect_data_files('ttkthemes')
hiddenimports = collect_submodules('ttkthemes')
