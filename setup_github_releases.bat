@echo off
echo ================================
echo  GitHub Releases Setup Script
echo ================================
echo.

echo Step 1: Restarting PowerShell session to update PATH...
echo Close this window and open a new PowerShell window, then run:
echo.
echo    gh auth login
echo.
echo Follow the prompts to authenticate with GitHub.
echo.
echo Step 2: After authentication, test with:
echo    gh repo view
echo.
echo Step 3: If that works, you can run:
echo    release.bat
echo.
echo ================================
echo  Manual Setup Instructions
echo ================================
echo.
echo If the above doesn't work:
echo 1. Close this PowerShell window
echo 2. Open a new PowerShell window as Administrator
echo 3. Run: gh auth login
echo 4. Choose "Login with a web browser"
echo 5. Follow the web authentication flow
echo 6. Test with: gh repo view
echo 7. Run: release.bat to create your first release
echo.
pause 