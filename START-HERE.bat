@echo off
rem First-time setup for the padel bot. Double-click this file.
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if defined PY %PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || set "PY="
if not defined PY (
  echo.
  echo Python 3.11 or newer is needed and was not found.
  echo   1. A download page opens now. Click the yellow "Download Python" button.
  echo   2. Run the downloaded installer.
  echo   3. IMPORTANT: tick "Add python.exe to PATH" at the bottom of the first screen.
  echo   4. Click "Install Now". When it is finished, double-click START-HERE.bat again.
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% -m padel_bot setup
echo.
pause
