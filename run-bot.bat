@echo off
rem Starts watching for free courts. Keep this window open; close it to stop.
rem If the bot stops (crash, no internet yet after starting Windows), it restarts after 1 minute.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
title Padel bot - watching (close this window to stop)
:again
%PY% -m padel_bot run
echo.
echo The bot stopped. Restarting in 60 seconds - close this window to stop it for good.
timeout /t 60 /nobreak >nul
goto again
