@echo off
:: SAMD Flasher - Windows launcher. Double-click to start.
title SAMD Flasher
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python not found. Install Python 3 from https://www.python.org/downloads/
    echo  and tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
python "%~dp0samd_flasher.py" %*
if errorlevel 1 (
    echo.
    echo  The application exited with an error.
    pause
)
