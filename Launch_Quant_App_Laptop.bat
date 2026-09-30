@echo off
setlocal

rem Always launch from the repository, even when this file is opened from Explorer.
set "APP_ROOT=%~dp0"
cd /d "%APP_ROOT%"

set "PYTHON_EXE="
if exist "%APP_ROOT%venv\Scripts\python.exe" set "PYTHON_EXE=%APP_ROOT%venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%APP_ROOT%.venv\Scripts\python.exe" set "PYTHON_EXE=%APP_ROOT%.venv\Scripts\python.exe"
if not defined PYTHON_EXE for %%I in (python.exe) do set "PYTHON_EXE=%%~$PATH:I"

if not defined PYTHON_EXE (
    echo Quant App could not find Python.
    echo Install the project dependencies, then try again.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -c "import PyQt5, pandas, sqlalchemy" >nul 2>&1
if errorlevel 1 (
    echo Quant App dependencies are missing from:
    echo %PYTHON_EXE%
    echo.
    echo Run: "%PYTHON_EXE%" -m pip install -r "%APP_ROOT%requirements.txt"
    pause
    exit /b 1
)

if /i "%~1"=="--check" (
    echo Quant App launcher check passed.
    echo Python: %PYTHON_EXE%
    exit /b 0
)

echo Starting Quant App on this laptop...
"%PYTHON_EXE%" "%APP_ROOT%main.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo Quant App stopped with error code %APP_EXIT_CODE%.
    pause
)

exit /b %APP_EXIT_CODE%
