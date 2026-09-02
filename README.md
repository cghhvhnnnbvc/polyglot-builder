# Polyglot Builder - 多格式文件拼接工具

![CI](https://github.com/cghhvhnnnbvc/polyglot-builder/actions/workflows/ci.yml/badge.svg) ![版本](https://img.shields.io/badge/version-1.0-blue)

将媒体/文档文件与加密 RAR 压缩包拼接为"多格式文件"——既是正常可播放的视频/图片/文档，改后缀名后又是可解压的 ZIP 压缩包。

## 下载

到 [Releases](https://github.com/cghhvhnnnbvc/polyglot-builder/releases) 下载 `PolyglotBuilder-vX.Y.Z-windows-x64.zip`，解压后**双击 `PolyglotBuilder.exe`** 即可（无需 Python 环境）。

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
| `.github/workflows/release.yml` | 自动发版 (推送 v* 标签即自动打包并发布到 Releases) |

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
| `外层文件` | 伪装文件路径 (必需, 除非用 `--gui`/`--batch`, 或无参数直接进 GUI) |
| `加密RAR` | AES-256 加密的 RAR 路径 (必需, 除非用 `--gui`/`--batch`) |
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
- 运行: **直接双击 `PolyglotBuilder.exe`** 即弹出 GUI (无参数自动进入), 无黑色控制台窗口一闪。
- spec 中 `console=False` (GUI 子系统), 彻底消除双击时的控制台闪烁; 代价是 exe 的 CLI 模式无控制台输出——需命令行/看日志时改用源码 `python polyglot_build.py ...` 或加 `--log-file`。

持续集成: 仓库含 `.github/workflows/ci.yml`, 在 push/PR 时于 Windows + Ubuntu × Python 3.10/3.12 矩阵自动跑 mypy 类型检查与 unittest (Linux 经 xvfb 实跑 GUI 用例)。

自动发版: 仓库含 `.github/workflows/release.yml`, 推送 `v*` 标签 (如 `git tag v1.0 && git push origin v1.0`) 即触发——自动校验标签与 `VERSION` 常量一致、跑测试、PyInstaller 打包、压缩为 zip, 并创建 GitHub Release 附上 `PolyglotBuilder-vX.Y.Z-windows-x64.zip`。

## 版本更新记录

### v1.0 (2026-09-02) — 首个公开发布版本

- **核心功能**: 将媒体/文档文件 (MP4/JPG/PDF/MP3/BMP) 与 AES-256 加密 RAR 拼接为“一文件两用”的 polyglot 文件——可正常播放/打开，改后缀为 `.zip` 又可解压
- **CLI**: `-y/--force` (非交互覆盖)、`--compress [high/medium/low]` (压缩外层视频, 需 ffmpeg)、`--batch MANIFEST` (批量构建)、`--log-file` (日志持久化)、`--deflate`、`--no-verify`、`--version`; Ctrl+C 干净取消并自动清理半成品
- **GUI**: tkinter 零依赖; 实时进度条与彩色分级日志、构建中可取消并恢复半成品、输出路径自定义、日志导出 `.txt`; 无参数运行 (含双击打包版 exe) 直达 GUI
- **大文件**: 8MB 分块流式处理, 内存占用恒定; 超过 4GB 自动 ZIP64 (数据描述符字段序已修复验证); ZIP 条目写入真实 DOS 时间戳 (取自 RAR mtime)
- **健壮性**: 构建后自动流式 CRC 校验 (大文件不卡 UI); 临时文件异常安全清理; ffmpeg 下载解压防 Zip Slip; 构建前预估输出体积与 RAR 魔数校验
- **质量与分发**: unittest 零依赖测试 (71 用例) + mypy 类型检查 + GitHub Actions CI (Windows/Ubuntu × Python 3.10/3.12 矩阵); PyInstaller onedir 打包 (`console=False`, 双击 exe 无黑色控制台闪烁)

> v1.0 之前的内部开发迭代 (v2.0~v3.0, 2026-07-26 同日完成) 已合并至本条目, 不再单独记录。

## 安全提示

- 本工具仅用于保护个人隐私文件
- 请勿用于传播违规、侵权或非法内容
- AES-256 加密的 RAR 使用强密码可抵抗暴力破解
- 公开分享时控制传播范围

## 许可

MIT License — 自由使用, 无担保
