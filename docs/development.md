# 开发与打包指南

## 环境要求

- **Python 3.6+** — 运行环境 (tkinter 已随 Python 自带)
- **WinRAR** — 仅用于创建 AES-256 加密 RAR (程序本身不依赖它)

无需安装任何第三方 Python 包。

## 文件清单

| 文件 | 说明 |
|------|------|
| `启动GUI.bat` | 双击直接启动图形界面 (零参数入口) |
| `polyglot_build.bat` | 启动器 (双模式：有参数走命令行，无参数走 GUI) |
| `polyglot_build.py` | 核心构建脚本 (GUI / CLI 共用核心逻辑) |
| `polyglot_gui.py` | 图形界面 (tkinter, Python 标准库) |
| `test_polyglot.py` | 回归测试 (unittest, 零依赖, 71 用例) |
| `polyglot_builder.spec` | PyInstaller 打包配置 (onedir, 可选) |
| `build_dist.bat` | 一键打包脚本 (装 PyInstaller → 跑测试 → 打包, 可选) |
| `.github/workflows/ci.yml` | GitHub Actions CI (mypy + unittest 多平台矩阵) |
| `.github/workflows/release.yml` | 自动发版 (推送 v* 标签即自动打包并发布到 Releases) |
| `docs/` | CLI 参数、技术原理等详细文档 |

## 运行测试与类型检查

```bash
python -m unittest test_polyglot          # 71 用例, 零依赖
python -m mypy                            # Windows 视角
python -m mypy --platform linux           # 模拟 CI ubuntu 视角
```

提交前请确保两者均通过（CI 会以同样标准检查 Windows + Ubuntu × Python 3.10/3.12）。

## 本地打包

若需分发给没有 Python 环境的用户，可用 PyInstaller 打包:

```bash
# 方式一: 一键脚本 (Windows, 自动装 PyInstaller + 跑测试 + 打包)
build_dist.bat

# 方式二: 手动
pip install pyinstaller
pyinstaller --noconfirm polyglot_builder.spec
```

- 采用 **onedir** 模式 (产物在 `dist/PolyglotBuilder/`), 因 ffmpeg 体积大, onefile 冷启动慢。
- 若源码目录下存在 `ffmpeg/`, 会被一并打包为**内置资源** (免运行时下载); 否则程序按需下载。
- spec 中 `console=False` (GUI 子系统): 双击 exe 无黑色控制台闪烁; 代价是 exe 的 CLI 模式无控制台输出，需日志时用 `--log-file` (详见 [CLI 参数详解](cli.md))。

## CI 与自动发版

- **CI** (`.github/workflows/ci.yml`): push/PR 时在 Windows + Ubuntu × Python 3.10/3.12 矩阵自动跑 mypy 与 unittest (Linux 经 xvfb 实跑 GUI 用例)。
- **自动发版** (`.github/workflows/release.yml`): 推送 `v*` 标签即触发:

  ```bash
  git tag v1.1 && git push origin v1.1
  ```

  流程: 校验标签与 `VERSION` 常量一致 → 跑测试 → PyInstaller 打包 → 压缩 zip → 创建 GitHub Release (自带中文说明)。

- **版本号单一来源**: `polyglot_build.py` 的 `VERSION` 常量; `polyglot_build.bat` 与 `README` 徽章需同步, `test_version_format` 等用例守护 bat 与常量一致。
