@echo off
REM tgcf Web UI batch script for Windows

REM Activate virtual environment if it exists
if exist .venv313\Scripts\activate.bat (
    call .venv313\Scripts\activate.bat
    echo Python 3.13 virtual environment activated
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    echo Virtual environment activated
)

echo Starting tgcf Web UI...
echo Press Ctrl+C to stop the server.
echo.

python run_web_ui.py

if errorlevel 1 (
    echo.
    echo Web UI exited with an error. Press any key to close.
    pause >nul
)
