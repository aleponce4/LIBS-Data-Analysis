# -*- mode: python ; coding: utf-8 -*-


block_cipher = None


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('element_database.csv', '.'), ('persistent_lines.csv', '.'), ('Icons/main_icon.ico', 'Icons'), ('Icons/Onteko_Logo.jpg', 'Icons'), ('Icons/clean_icon.png', 'Icons'), ('Icons/export_icon.png', 'Icons'), ('Icons/Import_icon.png', 'Icons'), ('Icons/plot_icon.png', 'Icons'), ('Icons/search_icon.png', 'Icons'), ('Icons/spectrum_icon.png', 'Icons'), ('Icons/savedata_icon.png', 'Icons')],
    hiddenimports=['ttkthemes', 'matplotlib'],
    hookspath=['./hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LIBS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['Icons\\main_icon.ico'],
)
