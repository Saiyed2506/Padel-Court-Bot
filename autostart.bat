@echo off
rem Makes the bot start by itself when you log in to Windows, and (if you agree)
rem keeps the laptop awake while plugged in. Undo with autostart-off.bat.
cd /d "%~dp0"
echo.
echo 1) Start the padel bot automatically when you log in to Windows?
set "OK="
set /p OK=   Type Y and press Enter for yes, just Enter for no: 
if /i not "%OK%"=="Y" goto power
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Padel bot.lnk'); $s.TargetPath = '%~dp0run-bot.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()"
if errorlevel 1 (echo    Could not create the startup entry.) else (echo    Done: the bot now starts when you log in - minimised in the taskbar.)
:power
echo.
echo 2) Keep the laptop awake while it is plugged in - also with the lid closed?
echo    (On battery nothing changes. The screen may still turn off.)
set "P="
set /p P=   Type Y and press Enter for yes, just Enter for no: 
if /i not "%P%"=="Y" goto done
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
echo    Done: plugged in, the laptop no longer sleeps.
:done
echo.
echo Tip: if Windows restarts for updates, it only starts the bot after you log in.
echo Settings - Accounts - Sign-in options - "Use my sign-in info to automatically finish setting up" helps.
echo.
pause
