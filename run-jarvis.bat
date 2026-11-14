@echo off
rem Starts Jarvis. Keep this window open; close it to stop Jarvis.
rem If Jarvis stops (crash, no internet yet after starting Windows), it restarts after 1 minute.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
title Jarvis - running (close this window to stop)
:again
%PY% -m jarvis run
echo.
echo Jarvis stopped. Restarting in 60 seconds - close this window to stop it for good.
timeout /t 60 /nobreak >nul
goto again
