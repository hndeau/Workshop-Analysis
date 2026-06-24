@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "APP=%SCRIPT_DIR%workshop_analysis.py"

if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    "%SCRIPT_DIR%.venv\Scripts\python.exe" "%APP%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%APP%" %*
    exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%APP%" %*
    exit /b %ERRORLEVEL%
)

where python3 >nul 2>nul
if not errorlevel 1 (
    python3 "%APP%" %*
    exit /b %ERRORLEVEL%
)

echo Python 3 was not found. Run setup.cmd to install prerequisites.
exit /b 1
