@echo off
chcp 65001 >nul 2>&1
title Polyglot Builder - 打包构建
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

where python >nul 2>&1
if not errorlevel 1 (set "PYTHON=python") else (
    where python3 >nul 2>&1
    if not errorlevel 1 (set "PYTHON=python3") else (
        echo [ERROR] 未找到 Python。下载: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

echo.
echo ========================================
echo   Polyglot Builder - 打包构建 (onedir)
echo ========================================
echo.

echo [1/3] 检查 PyInstaller...
"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo       未安装, 正在通过清华镜像安装...
    "%PYTHON%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
    if errorlevel 1 (
        echo [ERROR] PyInstaller 安装失败。
        pause
        exit /b 1
    )
)

echo [2/3] 运行回归测试 (打包前守护)...
"%PYTHON%" -m unittest test_polyglot
if errorlevel 1 (
    echo [WARN] 测试未全部通过。仍要继续打包吗?
    set /p GO="  继续? (y/N): "
    if /i not "%GO%"=="y" (
        echo 已中止。
        pause
        exit /b 1
    )
)

echo [3/3] 打包...
"%PYTHON%" -m PyInstaller --noconfirm polyglot_builder.spec
if errorlevel 1 (
    echo [ERROR] 打包失败。
    pause
    exit /b 1
)

echo.
echo ========================================
echo   完成! 产物: dist\PolyglotBuilder\
echo   运行: dist\PolyglotBuilder\PolyglotBuilder.exe --gui
echo ========================================
echo.
pause
exit /b 0
