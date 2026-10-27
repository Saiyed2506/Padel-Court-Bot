@echo off
rem Stops the bot from starting with Windows (doesn't touch power settings).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\Padel bot.lnk')"
echo The bot no longer starts with Windows. To change sleep settings back:
echo Settings - System - Power - Screen and sleep.
pause
