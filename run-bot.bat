@echo off
rem Starts watching for free courts. Keep this window open; close it to stop.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
title Padel bot - watching (close this window to stop)
%PY% -m padel_bot run
echo.
pause
