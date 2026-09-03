# Polyglot Builder - 多格式文件拼接工具

![CI](https://github.com/cghhvhnnnbvc/polyglot-builder/actions/workflows/ci.yml/badge.svg) ![版本](https://img.shields.io/badge/version-1.0-blue)

把媒体/文档文件与 AES-256 加密 RAR 拼接为一个"多格式文件"：**既能正常播放/打开，改后缀为 `.zip` 又能解压出加密 RAR**。

## 下载

到 [Releases](https://github.com/cghhvhnnnbvc/polyglot-builder/releases) 下载 `PolyglotBuilder-vX.Y.Z-windows-x64.zip`，解压后**双击 `PolyglotBuilder.exe`** 即可使用，**无需安装 Python**。

> 首次运行如遇 Windows SmartScreen 或杀毒软件提醒，属未签名开源程序的常见提示，选择"仍要运行"即可。

## 快速上手（3 步）

**第 1 步：用 WinRAR 制作加密 RAR**

1. 选中要保护的文件/文件夹 → 右键 → **"添加到压缩文件..."**
2. 格式选 **RAR**（不是 ZIP）
3. 点 **"设置密码..."** 输入密码
4. **务必勾选"加密文件名"** → 确定

（RAR 格式 + 密码 = AES-256 加密，无需额外设置）

**第 2 步：构建多格式文件**

双击 `PolyglotBuilder.exe`（打包版）或 `启动GUI.bat`（源码运行）：

1. 选择一个正常的视频/图片/文档作为**外层文件**
2. 选择刚做好的**加密 RAR**
3. 点 **"开始构建"**

**第 3 步：使用产物**

- 上传网盘：文件看起来就是一段正常视频/一张图
- 下载方：把后缀改为 `.zip` → 解压出加密 RAR → 输入密码拿到内容

## 外层文件格式兼容性

| 格式 | 兼容性 | 说明 |
|------|--------|------|
| MP4 / MKV / AVI | 高 | 播放器按 box 结构读取, 忽略尾部追加数据 |
| PDF | 高 | 解析器读到 `%%EOF` 标记后停止 |
| JPEG / BMP | 高 | 头部定义完整尺寸/结束标记, 多余字节被忽略 |
| MP3 / FLAC / OGG | 高 | 流式解码器按帧读取, 尾部数据自动跳过 |
| PNG / GIF | 中 | 大多数工具接受, 少数严格解析器会检查尾部 |
| WebP | 低 | RIFF 容器要求头部尺寸严格匹配文件大小 |
| DOCX / XLSX / PPTX | 不兼容 | 本身就是 ZIP 格式 |

## 常见问题

**Q: 支持多大的文件?**
A: 理论无上限。小于 4GB 用标准 ZIP, 超过自动启用 ZIP64。建议用 WinRAR 5.0+ 或最新版 7-Zip 解压。

**Q: 为什么压缩率是 0 甚至负数?**
A: RAR 本身已高度压缩, 默认 Store 模式不再二次压缩, 这是正常现象, 数据完全一致。

**Q: 上传到网盘安全吗?**
A: 可用于个人存储。文件哈希独一无二不在黑名单中, 外层内容正常不会触发 AI 审核报警, 内层 RAR 无密码无法打开。但公开分享请控制传播范围——若被举报人工审核, 文件结构仍可能被识别。

**Q: RAR 密码忘了怎么办?**
A: RAR AES-256 目前没有已知的有效破解方法, 请务必妥善保管密码。

**Q: 必须用命令行吗?**
A: 不用。图形界面覆盖日常全部场景; 命令行仅供批量/脚本场景, 参数详见 [CLI 参数详解](docs/cli.md)。

## 更多文档

- [CLI 参数详解](docs/cli.md) — 命令行完整参数、批量模式
- [技术原理](docs/technical.md) — 文件结构、为什么能"一文件两用"
- [开发与打包指南](docs/development.md) — 测试、本地打包、CI 与自动发版
- [更新记录](CHANGELOG.md)

## 安全提示

- 本工具仅用于保护个人隐私文件, 请勿用于传播违规、侵权或非法内容
- AES-256 加密的 RAR 使用强密码可抵抗暴力破解

## 许可

MIT License — 自由使用, 无担保
