@echo off
title Polyglot Builder

set "SCRIPT_DIR=%~dp0"

where python >nul 2>&1
if not errorlevel 1 (
    python "%SCRIPT_DIR%polyglot_build.py" --gui
    exit /b
)

where python3 >nul 2>&1
if not errorlevel 1 (
    python3 "%SCRIPT_DIR%polyglot_build.py" --gui
    exit /b
)

echo [ERROR] Python not found.
echo Download: https://www.python.org/downloads/
pause