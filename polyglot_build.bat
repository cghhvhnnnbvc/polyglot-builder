@echo off
chcp 65001 >nul 2>&1
@rem 版本号须与 polyglot_build.py 的 VERSION 常量保持一致
title Polyglot Builder v3.0
set "SCRIPT_DIR=%~dp0"

where python >nul 2>&1
if %errorlevel% equ 0 (set "PYTHON=python" & goto :check_args)
where python3 >nul 2>&1
if %errorlevel% equ 0 (set "PYTHON=python3" & goto :check_args)
echo [ERROR] Python not found.
echo Download: https://www.python.org/downloads/
pause
exit /b 1

:check_args
if not "%~1"=="" goto :cli

echo.
echo ========================================
echo   Polyglot Builder v3.0
echo ========================================
echo.
echo   [1] GUI (default)
echo   [2] Help
echo.
set /p MODE="  Select: "
if "%MODE%"=="2" goto :usage
"%PYTHON%" "%SCRIPT_DIR%polyglot_build.py" --gui
exit /b %errorlevel%

:cli
echo.
echo ========================================
echo   Polyglot Builder v3.0
echo ========================================
echo.
if "%~2"=="" (
    "%PYTHON%" "%SCRIPT_DIR%polyglot_build.py" %* --gui
) else (
    set "OUTPUT_ARG="
    if not "%~3"=="" set "OUTPUT_ARG=-o "%~3" "
    "%PYTHON%" "%SCRIPT_DIR%polyglot_build.py" %1 %2 %OUTPUT_ARG%
)
echo.
pause
exit /b 0

:usage
echo.
echo Usage:
echo   polyglot_build.bat           - Launch GUI
echo   polyglot_build.bat --gui     - Launch GUI
echo   polyglot_build.bat ^<outer^> ^<rar^> [output]
echo.
echo Examples:
echo   polyglot_build.bat video.mp4 game.rar
echo   polyglot_build.bat photo.jpg secret.rar -o out.jpg
echo.
pause
exit /b 0