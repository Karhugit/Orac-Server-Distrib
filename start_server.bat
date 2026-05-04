@echo off
cd /d "%~dp0"

echo ============================================
echo   Orac Server Launcher
echo ============================================
echo.

:: Check if venv exists
if exist "venv\Scripts\python.exe" (
    goto :start_server
)

echo First run detected - setting up environment...
echo.

:: Find Python
where python >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python
    goto :found_python
)

where python3 >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=python3
    goto :found_python
)

where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_CMD=py
    goto :found_python
)

echo ERROR: Python not found. Please install Python 3.8+ from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
pause
exit /b 1

:found_python
echo Found Python: %PYTHON_CMD%

:: Check Python version
%PYTHON_CMD% --version
echo.

:: Create virtual environment
echo Creating virtual environment...
%PYTHON_CMD% -m venv venv
if %errorlevel% neq 0 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)
echo Virtual environment created successfully.
echo.

:: Install requirements
echo Installing dependencies...
venv\Scripts\pip.exe install -r requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
echo Dependencies installed successfully.
echo.

:: Check for config.json
if not exist "config.json" (
    if exist "config.example.json" (
        echo Copying config.example.json to config.json...
        copy config.example.json config.json >nul
        echo.
        echo IMPORTANT: Please edit config.json with your API keys before running.
        echo            - TRAKT client_id and client_secret
        echo            - TMDB api_key
        echo.
        pause
        exit /b 0
    )
)

echo Setup complete!
echo.

:start_server
echo Checking for updated dependencies...
venv\Scripts\pip.exe install -q -r requirements.txt
echo Starting Orac Server...
venv\Scripts\python.exe -W ignore::SyntaxWarning run_server.py
pause
