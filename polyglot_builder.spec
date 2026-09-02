# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 (onedir 模式)。

用法:
    pip install pyinstaller
    pyinstaller --noconfirm polyglot_builder.spec
    (或直接双击 build_dist.bat 一键完成)

产物位于 dist/PolyglotBuilder/, 运行 dist/PolyglotBuilder/PolyglotBuilder.exe。

设计说明:
  - 选 onedir 而非 onefile: ffmpeg 体积大 (上百 MB), onefile 每次启动都要
    解压到临时目录, 冷启动慢; onedir 直接读目录, 启动快。
  - ffmpeg/ 若存在于源码目录会被一并打包为内置资源 (免运行时下载);
    不存在则程序仍可通过 find_ffmpeg()/download_ffmpeg() 按需下载。
  - 入口为 polyglot_build.py (含 CLI main 与 --gui 分发);
    polyglot_gui 为惰性导入, 用 hiddenimports 显式纳入。
  - console=False (GUI 子系统): 双击 exe 直接弹 GUI, 彻底消除黑色控制台一闪;
    代价是 exe 的 CLI 模式无控制台输出 (改用 --log-file 看日志, 或用源码跑 CLI)。
"""
import os

# SPECPATH 由 PyInstaller 注入, 为本 spec 文件所在目录
SRC_DIR = os.path.abspath(SPECPATH)

# 条件性包含 ffmpeg/ 目录 (存在则打包为内置资源)
datas = []
ffmpeg_dir = os.path.join(SRC_DIR, 'ffmpeg')
if os.path.isdir(ffmpeg_dir):
    datas.append((ffmpeg_dir, 'ffmpeg'))

a = Analysis(
    ['polyglot_build.py'],
    pathex=[SRC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=['polyglot_gui'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PolyglotBuilder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 子系统: 双击 exe 无黑色控制台闪烁; CLI 输出改走 --log-file
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PolyglotBuilder',
)
