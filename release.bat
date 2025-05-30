@echo off
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

:: Activate virtual environment if it exists
if exist "LIBS_venv\Scripts\activate.bat" (
    echo Activating virtual environment...
    call LIBS_venv\Scripts\activate.bat
)

python compile.py
if %errorlevel% neq 0 (
    echo Error: Compilation failed
    pause
    exit /b 1
)

echo.
echo Committing changes to git...
echo ================================
git add *.py
git commit -m "Release %version%: Update with new features"
if %errorlevel% neq 0 (
    echo Warning: Git commit may have failed (could be no changes)
)

git push
if %errorlevel% neq 0 (
    echo Error: Git push failed
    pause
    exit /b 1
)

echo.
echo Finding compiled executable...
echo ================================

:: Find the most recent compiled folder using Windows commands
set newest_folder=
for /f "delims=" %%i in ('dir "Compiled version\compiled_*" /b /ad /o-d 2^>nul') do (
    if not defined newest_folder set newest_folder=%%i
)

if "%newest_folder%"=="" (
    echo Error: No compiled folder found
    echo Available folders:
    dir "Compiled version" /b
    pause
    exit /b 1
)

set exe_path="Compiled version\%newest_folder%\LIBS.exe"

if not exist %exe_path% (
    echo Error: Executable not found at %exe_path%
    echo Contents of %newest_folder%:
    dir "Compiled version\%newest_folder%" /b
    pause
    exit /b 1
)

echo Found executable: %exe_path%

echo.
echo Creating GitHub release...
echo ================================

:: Create release notes
set release_notes=- Added prominence filter for better peak detection^

- Added laser removal feature (532.63 nm)^

- Improved spectral analysis capabilities

gh release create %version% --title "LIBS Data Analysis %version%" --notes "%release_notes%" %exe_path%

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
echo https://github.com/YOUR_USERNAME/LIBS-Data-Analysis/releases/latest
echo.
echo Don't forget to update your README.md download link to:
echo https://github.com/YOUR_USERNAME/LIBS-Data-Analysis/releases/latest
echo.
pause 