@echo off
rem One-off check: finds where Playtomic serves event data. Screenshot the result for Claude.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -m padel_bot explore-events https://app.playtomic.com/tournaments/30edfbd8-8f5f-4b74-8075-500e4caa7c78
echo.
pause
