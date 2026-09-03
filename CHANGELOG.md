# 更新记录 (Changelog)

## v1.0 (2026-09-02) — 首个公开发布版本

- **核心功能**: 将媒体/文档文件 (MP4/JPG/PDF/MP3/BMP) 与 AES-256 加密 RAR 拼接为"一文件两用"的 polyglot 文件——可正常播放/打开，改后缀为 `.zip` 又可解压
- **CLI**: `-y/--force` (非交互覆盖)、`--compress [high/medium/low]` (压缩外层视频, 需 ffmpeg)、`--batch MANIFEST` (批量构建)、`--log-file` (日志持久化)、`--deflate`、`--no-verify`、`--version`; Ctrl+C 干净取消并自动清理半成品
- **GUI**: tkinter 零依赖; 实时进度条与彩色分级日志、构建中可取消并恢复半成品、输出路径自定义、日志导出 `.txt`; 无参数运行 (含双击打包版 exe) 直达 GUI
- **大文件**: 8MB 分块流式处理, 内存占用恒定; 超过 4GB 自动 ZIP64 (数据描述符字段序已修复验证); ZIP 条目写入真实 DOS 时间戳 (取自 RAR mtime)
- **健壮性**: 构建后自动流式 CRC 校验 (大文件不卡 UI); 临时文件异常安全清理; ffmpeg 下载解压防 Zip Slip; 构建前预估输出体积与 RAR 魔数校验
- **质量与分发**: unittest 零依赖测试 (71 用例) + mypy 类型检查 + GitHub Actions CI (Windows/Ubuntu × Python 3.10/3.12 矩阵); PyInstaller onedir 打包 (`console=False`, 双击 exe 无黑色控制台闪烁)

> v1.0 之前的内部开发迭代 (v2.0~v3.0, 2026-07-26 同日完成) 已合并至本条目, 不再单独记录。
