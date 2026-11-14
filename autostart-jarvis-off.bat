@echo off
rem Stops Jarvis from starting with Windows.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\Jarvis.lnk')"
echo Jarvis no longer starts with Windows.
pause
