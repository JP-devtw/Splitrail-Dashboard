@echo off
if "%~1"=="" (python "%~dp0splitrail-dashboard.py" --serve) else (python "%~dp0splitrail-dashboard.py" %*)
