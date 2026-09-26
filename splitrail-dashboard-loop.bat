@echo off
:loop
call "%~dp0splitrail-dashboard.bat" --no-open
timeout /t 300 /nobreak >nul
goto loop
