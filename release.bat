@echo off
set app_name=LIBS Software
echo ================================
echo    LIBS Data Analysis Release    
echo ================================
echo.

:: Get version from user
set /p version="Enter version (e.g., v2.1): "

:: Validate version format
if "%version%"=="" (
    echo Error: Version cannot be empty
    pause
    exit /b 1
)

echo.
echo Building executable...
echo ================================

:: Activate release virtual environment
if exist "LIBS_venv\Scripts\activate.bat" (
    echo Activating virtual environment...
    call LIBS_venv\Scripts\activate.bat
) else (
    echo Error: LIBS_venv not found. Create it before releasing.
    pause
    exit /b 1
)

python compile.py
if %errorlevel% neq 0 (
    echo Error: Compilation failed
    pause
    exit /b 1
)

echo.
echo Checking for uncommitted changes...
echo ================================
REM Releases are cut from an already-committed tree. The script used to stage a
REM glob from the repo root and commit "Update with new features" on your behalf,
REM which both missed the src/ layout and wrote a commit message that said nothing.
git diff --quiet && git diff --cached --quiet
if %errorlevel% neq 0 (
    echo Error: working tree has uncommitted changes.
    echo Commit them yourself, with a message describing the release, then re-run.
    exit /b 1
)
if %errorlevel% neq 0 (
    echo Error: Git push failed
    pause
    exit /b 1
)

echo.
echo Finding compiled release artifacts...
echo ================================

:: Find the most recent onedir build folder using Windows commands
set newest_dir=
for /f "delims=" %%i in ('dir "Compiled version\compiled_*" /b /ad /o-d 2^>nul') do (
    if not defined newest_dir (
        echo %%i| findstr /r /c:"_dir$" >nul
        if not errorlevel 1 set newest_dir=%%i
    )
)

if "%newest_dir%"=="" (
    echo Error: No onedir build folder found
    echo Available folders:
    dir "Compiled version" /b
    pause
    exit /b 1
)

set zip_path="Compiled version\%newest_dir%\%app_name%.zip"

if not exist %zip_path% (
    echo Error: Primary onedir zip not found at %zip_path%
    echo Contents of %newest_dir%:
    dir "Compiled version\%newest_dir%" /b
    pause
    exit /b 1
)

set newest_onefile=%newest_dir:_dir=%
set exe_path="Compiled version\%newest_onefile%\%app_name%.exe"
set release_assets=%zip_path%

if exist %exe_path% (
    set release_assets=%release_assets% %exe_path%
    echo Found primary artifact: %zip_path%
    echo Found fallback artifact: %exe_path%
) else (
    echo Found primary artifact: %zip_path%
    echo Warning: Fallback onefile executable not found at %exe_path%
)

echo.
echo Creating GitHub release...
echo ================================

:: Create release notes
set release_notes=- Model-agnostic spectrometer support (any Ocean Optics model)^

- Dynamic device capabilities detection (pixels, trigger modes, integration limits)^

- Simulation profiles for USB4000, QEPro, HDX, and Generic^

- UI adapts dynamically to connected device capabilities^

- Architecture prepared for future multi-brand support^

- Recommended download: zipped one-folder build for faster startup

gh release create %version% --title "LIBS Data Analysis %version%" --notes "%release_notes%" %release_assets%

if %errorlevel% neq 0 (
    echo Error: GitHub release creation failed
    echo Make sure you're authenticated with: gh auth login
    pause
    exit /b 1
)

echo.
echo ================================
echo    Release %version% Complete!    
echo ================================
echo.
echo Your release is now available at:
echo https://github.com/aleponce4/libs-spectroscopy-workbench/releases/latest
echo.
echo Don't forget to update your README.md download link to:
echo https://github.com/aleponce4/libs-spectroscopy-workbench/releases/latest
echo.
pause
