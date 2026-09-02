# Polyglot Builder - 多格式文件拼接工具

将媒体/文档文件与加密 RAR 压缩包拼接为"多格式文件"——既是正常可播放的视频/图片/文档，改后缀名后又是可解压的 ZIP 压缩包。

## 功能

- 一个 `.mp4` 视频也能当 `.zip` 解压
- 一张 `.jpg` 图片也能当 `.zip` 解压
- 一份 `.pdf` 文档也能当 `.zip` 解压
- 一段 `.mp3` 音频也能当 `.zip` 解压
- `.bmp` 位图也能当 `.zip` 解压

## 文件清单

| 文件 | 说明 |
|------|------|
| `启动GUI.bat` | 双击直接启动图形界面 (零参数入口) |
| `polyglot_build.bat` | 启动器 (双模式：有参数走命令行，无参数走 GUI) |
| `polyglot_build.py` | 核心构建脚本 (GUI / CLI 共用核心逻辑) |
| `polyglot_gui.py` | 图形界面 (tkinter, Python 标准库) |
| `test_polyglot.py` | 回归测试 (unittest, 零依赖, `python -m unittest test_polyglot`) |
| `polyglot_builder.spec` | PyInstaller 打包配置 (onedir, 可选) |
| `build_dist.bat` | 一键打包脚本 (装 PyInstaller → 跑测试 → 打包, 可选) |
| `.github/workflows/ci.yml` | GitHub Actions CI (mypy + unittest 多平台矩阵) |

## 安装要求

- **Python 3.6+** — 运行环境 (提供 tkinter GUI 支持)
- **WinRAR** — 用于提前创建 AES-256 加密的 RAR 压缩包 (RARLAB 官网: https://www.win-rar.com/)

无需安装任何第三方 Python 包。tkinter 已随 Python 自带。

## 使用方法

### 方式一: 图形界面 (推荐)

**双击 `启动GUI.bat`** 或 `polyglot_build.bat` (无参数时默认进入 GUI)。

图形界面操作步骤：

1. **外层文件** — 点"浏览..."选择伪装用的媒体/文档文件 (`mp4`/`pdf`/`jpg`/`mp3` 等)
2. **加密 RAR** — 点"浏览..."选择你已用 WinRAR 压缩并设置密码的 `.rar` 文件
3. **输出文件名** — 可选。点"另存为..."选择保存位置并自定义文件名；留空则与外层文件同名
4. 点 **"开始构建"**，等待约 1-2 分钟（构建中可点 **"取消"** 中止并自动清理半成品）
5. 完成后弹窗显示文件路径和大小

进度实时显示在进度条上，日志面板实时输出处理信息（构建与校验阶段均有进度反馈）。日志面板右上角的“导出”按钮可将日志另存为 `.txt`，便于事后排查或反馈问题。

### 方式二: 命令行

```bash
polyglot_build.py <外层文件> <加密RAR> [-o 输出文件] [选项]
```

参数说明：

| 参数 | 说明 |
|------|------|
| `外层文件` | 伪装文件路径 (必需, 除非使用 `--gui`) |
| `加密RAR` | AES-256 加密的 RAR 路径 (必需, 除非使用 `--gui`) |
| `-o / --output` | 输出文件路径 (默认与外层文件同名) |
| `--gui` | 启动图形界面 |
| `-q / --quiet` | 静默模式, 不显示进度 |
| `--deflate` | 使用 Deflate 压缩 (默认 Store 不压缩, RAR 已压缩无需再压) |
| `--no-verify` | 跳过构建后 ZIP 完整性校验 |
| `-y / --force` | 强制覆盖已存在的输出文件, 不交互询问 (适合脚本/CI) |
| `--compress [QUALITY]` | 压缩外层视频以减小体积、提高隐蔽性 (需 ffmpeg); QUALITY: `high`/`medium`/`low`, 默认 `medium` |
| `--batch MANIFEST` | 批量模式: 指定清单文本文件, 每行 `外层\|RAR[\|输出]`; 忽略空行与 `#` 注释行, 任一条失败不中断后续, 末尾汇总成败 |
| `--log-file PATH` | 将日志额外持久化到指定文件 (追加模式, UTF-8), 便于事后排查 |
| `--version` | 显示版本号 |

> Ctrl+C 可随时中止 CLI 构建, 半成品输出会被自动清理。

示例：

```bash
python polyglot_build.py video.mp4 game.rar
python polyglot_build.py photo.jpg secret.rar -o result.jpg --deflate
python polyglot_build.py document.pdf data.rar --output D:\upload\doc.pdf -y
python polyglot_build.py --batch tasks.txt --log-file build.log -y
polyglot_build.py --gui
```

批量清单 `tasks.txt` 示例 (每行 `外层|RAR[|输出]`, `#` 开头为注释):

```text
# 第三段输出可省略, 缺省时与外层同名
D:\media\v1.mp4|D:\rar\s1.rar|D:\out\v1.mp4
D:\media\v2.mp4|D:\rar\s2.rar
```

## 完整工作流

```
步骤 1: 准备外层文件 (一段正常的 MP4 视频 / 一张图 / 一个 PDF)
         ↓
步骤 2: WinRAR 压缩目标内容 → xxx.rar
         · 压缩格式选 RAR (不要选 ZIP)
         · 设置密码
         · 勾选"加密文件名"
         ↓
步骤 3: 运行 Polyglot Builder
         · GUI: 双击"启动GUI.bat"，选两个文件，点"开始构建"
         · CMD: python polyglot_build.py 外层文件 加密RAR
         ↓
步骤 4: 生成的文件 (.mp4、.jpg、.pdf 等) 即可上传到网盘
```

下载方的解压流程：

```
extension.mp4  →  改后缀为 .zip  →  WinRAR 打开  →  解压出 xxx.rar
                                                          ↓
                                              输入密码解压  →  得到游戏文件
```

## 技术原理

### 文件结构

```
┌─────────────────────────┬──────────────────────────────────────┐
│   外层文件 (~X MB)       │         ZIP 压缩包 (~Y GB)            │
│                         │                                      │
│  ftyp → free → mdat     │  本地文件头 → Deflate压缩数据 →       │
│  → moov (MP4示例)       │  数据描述符 → 中央目录 → EOCD         │
│                         │                                      │
│  ↑ 播放器/PDF阅读器      │                        ↑ WinRAR 从尾部 │
│  从头读取，忽略尾部       │                          开始反向解析  │
└─────────────────────────┴──────────────────────────────────────┘
```

### 为什么能同时工作

- **外层文件**: 播放器/阅读器头读取 (如 MP4 解析 box 结构、PDF 读到 `%%EOF`), 读完所需数据即停止, 忽略尾部追加的 ZIP 数据
- **ZIP 结构**: WinRAR/7-Zip 从文件末尾搜索 EOCD 签名 (`PK\x05\x06`), 找到后再通过中央目录定位文件, 完全忽略文件头部
- **RAR AES-256**: 即使 ZIP 层被解压, 拿到的是加密的 RAR, 没有密码无法打开

### 双层加密设计

- **外层 ZIP 不设密码** — 降低被网盘 AI 抽帧分析的风险；任何人都能尝试解压 ZIP, 这是"隐蔽性"的一部分
- **内层 RAR 使用 AES-256** — 真正的安全层；WinRAR 的 AES-256 加密强度远高于传统 ZipCrypto, 且每个密码产生的哈希值不同, 无法被网盘平台哈希黑名单识别

### 大文件支持

- 使用 ZIP **数据描述符 (Data Descriptor)** 实现流式压缩, 无需预先知道压缩后大小
- 分块读取 8MB, 内存占用恒定, 不随文件大小变化
- 自动启用 **ZIP64** (单文件超过 4GB 时), WinRAR 5.0+ / 7-Zip 均完全支持

## 外层文件格式兼容性

| 格式 | 兼容性 | 说明 |
|------|--------|------|
| MP4 / MKV / AVI | 高 | 播放器按 box 结构读取, 忽略尾部追加数据 |
| PDF | 高 | 解析器读到 `%%EOF` 标记后停止 |
| JPEG | 高 | 解码器遇到 EOI 标记 `FF D9` 后停止 |
| BMP | 高 | 文件头已定义完整尺寸, 多余字节被忽略 |
| MP3 / FLAC / OGG | 高 | 流式解码器按帧读取, 尾部数据自动跳过 |

| 格式 | 兼容性 | 说明 |
|------|--------|------|
| PNG / GIF | 中 | 大多数工具接受, 少数严格解析器会检查文件尾部 |
| WebP | 低 | RIFF 容器要求头部声明尺寸严格匹配文件大小 |
| DOCX / XLSX / PPTX | 不兼容 | 本身就是 ZIP 格式 |

## WinRAR 加密设置指南

1. 选中目标文件/文件夹 → 右键 → **"添加到压缩文件..."**
2. 压缩文件格式选 **RAR** (不是 ZIP!)
3. 点击右边的 **"设置密码..."** 按钮
4. 输入密码并确认
5. **务必勾选"加密文件名"** — 这样没有密码的人连压缩包里有什么文件都看不到
6. 点击确定, 开始压缩

> WinRAR 的 RAR 格式默认使用 AES-256-CBC 加密。只要用的是 RAR 格式+设了密码, 就是 AES-256。

## 常见问题

**Q: 支持多大的文件?**
A: 理论无上限。小于 4GB 用标准 ZIP, 大于等于 4GB 自动启用 ZIP64。建议使用 WinRAR 5.0+ 或最新版 7-Zip 解压。

**Q: 为什么压缩率这么低甚至是负数?**
A: RAR 文件本身已高度压缩, 再用 Deflate 几乎无法进一步压缩, 反而会因为 ZIP 格式开销而略微变大。这是正常现象, 解压后数据完全一致。

**Q: 上传到百度网盘/夸克网盘/迅雷网盘安全吗?**
A: 可以用于个人存储。算法审核层面:
- 整个 polyglot 文件的哈希值独一无二, 不在任何违规文件黑名单中
- 外层视频/图片/文档内容正常, AI 内容分析不会报警
- 内层 RAR 使用 AES-256 加密, 没有密码平台无法打开

注意: 公开私密分享时控制传播范围, 如果被人举报导致人工审核, 文件结构仍可能被识别。

**Q: 解压后的 RAR 密码忘记了怎么办?**
A: RAR AES-256 加密目前没有已知的有效破解方法。请妥善保管密码。

**Q: 外层文件可以是任意格式吗?**
A: 推荐使用 MP4 / PDF / JPEG / BMP / MP3。WebP 不能使用, PNG 和 GIF 部分工具可能不识别。详见"外层文件格式兼容性"表。

## 打包发布 (可选)

源码可直接用 Python 运行, 无需打包。若需分发给没有 Python 环境的用户, 可用 PyInstaller 打包为独立可执行程序:

```bash
# 方式一: 一键脚本 (Windows, 自动装 PyInstaller + 跑测试 + 打包)
build_dist.bat

# 方式二: 手动
pip install pyinstaller
pyinstaller --noconfirm polyglot_builder.spec
```

- 采用 **onedir** 模式 (产物在 `dist/PolyglotBuilder/`), 因 ffmpeg 体积大, onefile 冷启动慢。
- 若源码目录下存在 `ffmpeg/`, 会被一并打包为**内置资源** (免运行时下载); 否则程序仍会按需下载。
- 运行: `dist/PolyglotBuilder/PolyglotBuilder.exe --gui`。
- spec 中 `console=True` 保留控制台以兼容 CLI; 若只发布 GUI, 可改为 `False` 去掉控制台窗口。

持续集成: 仓库含 `.github/workflows/ci.yml`, 在 push/PR 时于 Windows + Ubuntu × Python 3.10/3.12 矩阵自动跑 mypy 类型检查与 unittest (Linux 经 xvfb 实跑 GUI 用例)。

## 版本更新记录

### v3.0 (2026-07-26)
- **版本号统一**: 运行时代码、GUI、启动器与本文档统一标记为 v3.0，消除历史版本号混乱 (此前 bat 标 v2.0、本文档标 v2.2)
- **CLI 增强**: 加 `--version`、`-y/--force` (非交互覆盖, 适合 CI)、`--compress [QUALITY]` (压缩外层视频)、`--batch MANIFEST` (批量构建)、`--log-file PATH` (日志持久化)；Ctrl+C 干净取消并清理半成品 (退出码 130)
- **GUI 可取消构建**: 新增红色"取消"按钮, 构建中可中止并自动恢复/清理半成品输出；`RoundedButton` 支持自定义悬停/按下色
- **ZIP64 路径修复**: 修复 `build_zip64_eocd_locator` 格式 (`<IQQ` → `<IIQI`, 此前 >4GB 文件直接崩溃)；分离 `ZIP64_MARKER` 与阈值使 ZIP64 路径可测；本地头 extra 不再误含 offset 字段
- **ZIP64 数据描述符**: 修复字段顺序 (`<IQQQ` → `<IIQQ`, signature→CRC→compressed→uncompressed)；Deflate 模式下本地头 compressed 传 0 不写未知值
- **临时文件异常安全**: 清理改用 `_auto_remove` / `_cancel_scope` contextmanager, 构建异常或取消时也清理
- **流式循环重构**: 抽取 `_stream_copy` 统一复制外层/Deflate/Store 三段循环, 均带 0.2s 进度节流 (首帧即报)
- **校验进度**: `verify_polyglot` 改为分块流式 CRC 校验, 大文件校验时不再 UI 假死
- **类型注解**: 核心函数补充类型注解 (基于 `from __future__ import annotations`)
- **视频压缩真实进度**: `compress_video` 改用 `-progress pipe:1 -nostats` 按行解析 `out_time` 上报百分比; stderr 重定向到临时文件修复长编码管道写满导致的死锁隐患
- **安全加固**: ffmpeg 下载解压改用 `_safe_extractall` 防 Zip Slip (校验 realpath 落在目标目录内)
- **隐蔽性增强**: ZIP 条目写入真实 DOS 时间戳 (取自 RAR 的 mtime, 避免 1980-00-00 异常特征); 构建前预估输出体积; `_validate_rar` 校验 RAR 魔数 (非 RAR 仅警告不中断)
- **日志持久化**: CLI `--log-file` 挂 FileHandler (追加/UTF-8/带时间戳级别); GUI 日志区新增"导出"按钮另存为 .txt
- **打包分发**: 新增 `polyglot_builder.spec` (PyInstaller onedir, 条件内置 ffmpeg) 与 `build_dist.bat` 一键打包脚本
- **测试与质量**: 新增 `test_polyglot.py` (unittest, 零依赖, 69 用例) 守护数据描述符/端到端轮转/ZIP64 边界/取消/CLI 分发 (--compress/--gui/--batch)/verify 正负/输出对话框/自动同步/版本一致性/进度解析/DOS 时间戳/RAR 校验/日志持久化；测试即发现并修复了两个 ZIP64 真实 bug
- **仓库规范**: 新增 `.gitignore` (排除 `__pycache__/` `*.pyc` `build/` `dist/` 等)、`LICENSE` (MIT) 与 `.github/workflows/ci.yml` (Windows+Ubuntu × py3.10/3.12 矩阵跑 mypy + unittest)

### v2.2 (2026-07-26)
- **UI 全面升级**: 窗口默认从 680×560 扩大到 880×700, 所有字号全面提升 (标题 24px / 标签 14px / 按钮 16px 等)
- **输出文件名自定义**: 新增"另存为..."对话框, 可自由选择保存位置和自定义文件名
- **纵向拉伸优化**: 布局改用 grid + row weight, 窗口拉伸时只有日志区纵向扩展, 文件区/按钮/进度条保持固定
- **文件选择卡片化**: 文件选择区带分组边框和更大 padding, 视觉层次更清晰

### v2.1 (2026-07-26)
- 增加 `--gui` 图形界面入口 (基于 tkinter, 零依赖)
- 三种方式启动 GUI: `polyglot_build.bat` (无参数)、`polyglot_build.py --gui`、双击 `启动GUI.bat`
- GUI 包含: 三个文件选择器 (对应格式过滤)、实时进度条、深色日志面板 (彩色分级高亮)
- 构建过程在后台线程执行, UI 不卡顿

### v2.0 (2026-07-26)
- 首个公开版本
- 支持命令行模式
- 支持大文件流式处理 (8MB 分块)
- 自动 ZIP64 (超过 4GB)
- 数据描述符 (Data Descriptor) 支持
- 修复 EOCD 中央目录偏移截断 Bug (混用 16-bit 和 32-bit 阈值)
- 修复输出与输入相同路径时的截断问题 (增加临时文件保护)

## 安全提示

- 本工具仅用于保护个人隐私文件
- 请勿用于传播违规、侵权或非法内容
- AES-256 加密的 RAR 使用强密码可抵抗暴力破解
- 公开分享时控制传播范围

## 许可

MIT License — 自由使用, 无担保
