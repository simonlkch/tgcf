@echo off
REM tgcf CLI batch script for Windows

REM Activate virtual environment if it exists
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    echo Virtual environment activated
)

REM Check for command line arguments
if "%1"=="help" goto help
if "%1"=="run-live" goto run_live
if "%1"=="run-past" goto run_past
if "%1"=="run-live-verbose" goto run_live_verbose
if "%1"=="run-past-verbose" goto run_past_verbose

:help
    echo tgcf CLI Usage:
    echo.    tgcf_cli.bat help                - Show this help message
    echo.    tgcf_cli.bat run-live            - Run tgcf in live mode
    echo.    tgcf_cli.bat run-past            - Run tgcf in past mode
    echo.    tgcf_cli.bat run-live-verbose    - Run tgcf in live mode with verbose output
    echo.    tgcf_cli.bat run-past-verbose    - Run tgcf in past mode with verbose output
    goto end

:run_live
    echo Running tgcf in live mode...
    python run_tgcf.py live
    goto end

:run_past
    echo Running tgcf in past mode...
    python run_tgcf.py past
    goto end

:run_live_verbose
    echo Running tgcf in live mode with verbose output...
    python run_tgcf.py live --loud
    goto end

:run_past_verbose
    echo Running tgcf in past mode with verbose output...
    python run_tgcf.py past --loud
    goto end

:end
    if exist .venv\Scripts\deactivate.bat (
        call .venv\Scripts\deactivate.bat
        echo Virtual environment deactivated
    )