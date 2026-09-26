@echo off
:loop
call "%~dp0splitrail-dashboard.bat" --no-open
timeout /t 900 /nobreak >nul
goto loop
