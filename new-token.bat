@echo off
rem After /revoke in BotFather: paste the new Telegram token. Wishes, club, friends and login stay.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -m padel_bot new-token
echo.
pause
