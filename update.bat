@echo off
rem Downloads the newest version of the bot. Keeps your .env, config.toml, club and wishes.
cd /d "%~dp0"
echo Close the bot window (run-bot.bat) first if it is running.
pause
set "ZIP=%TEMP%\padel-bot-update.zip"
set "TMPDIR=%TEMP%\padel-bot-update"
echo Downloading the newest version...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Saiyed2506/Padel-Court-Bot/archive/refs/heads/ccr-7f8b936d-chkl4x.zip' -OutFile $env:ZIP; if (Test-Path $env:TMPDIR) { Remove-Item $env:TMPDIR -Recurse -Force }; Expand-Archive -Path $env:ZIP -DestinationPath $env:TMPDIR"
if errorlevel 1 (
  echo Download failed. Check your internet connection and try again.
  pause
  exit /b 1
)
set "SRC="
for /d %%D in ("%TMPDIR%\*") do set "SRC=%%D"
if not defined SRC (
  echo The download looks empty. Try again later.
  pause
  exit /b 1
)
rem Program files only: your .env, config.toml, club.json, state.json and wishes.json stay as they are.
robocopy "%SRC%\padel_bot" "padel_bot" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
robocopy "%SRC%\tests" "tests" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
robocopy "%SRC%" "." /XF config.toml update.bat .env /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
echo.
echo Updated! If the bot is running, close its window and start run-bot.bat again.
pause
exit /b 0
:failed
echo Copying the new files failed. Close the bot window and try again.
pause
exit /b 1
