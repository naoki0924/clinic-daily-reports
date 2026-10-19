@echo off
cd /d "%~dp0.."
"C:\Users\okita\.venvs\clinic-reports\Scripts\pythonw.exe" -m app.cli serve --port 8100
