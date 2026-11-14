@echo off
rem Makes Jarvis start by itself when you log in to Windows. Undo with autostart-jarvis-off.bat.
rem To keep the laptop awake while plugged in, use the padel bot's autostart.bat (question 2).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Jarvis.lnk'); $s.TargetPath = '%~dp0run-jarvis.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()"
if errorlevel 1 (echo Could not create the startup entry.) else (echo Done: Jarvis now starts when you log in - minimised in the taskbar.)
pause
