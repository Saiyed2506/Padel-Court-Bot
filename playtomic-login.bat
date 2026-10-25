@echo off
rem Log in to Playtomic once so the bot can read Open Play events. Your password is not saved.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -m padel_bot login
echo.
pause
