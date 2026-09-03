# CLI 参数详解

## 基本用法

```bash
python polyglot_build.py <外层文件> <加密RAR> [-o 输出文件] [选项]
```

无参数运行时自动进入图形界面（与 `--gui` 等效）。

## 参数说明

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
| `--batch MANIFEST` | 批量模式: 指定清单文本文件 (见下文) |
| `--log-file PATH` | 将日志额外持久化到指定文件 (追加模式, UTF-8), 便于事后排查 |
| `--version` | 显示版本号 |

> Ctrl+C 可随时中止 CLI 构建, 半成品输出会被自动清理 (退出码 130)。

## 示例

```bash
python polyglot_build.py video.mp4 game.rar
python polyglot_build.py photo.jpg secret.rar -o result.jpg --deflate
python polyglot_build.py document.pdf data.rar --output D:\upload\doc.pdf -y
python polyglot_build.py --batch tasks.txt --log-file build.log -y
python polyglot_build.py --gui
```

## 批量模式 (--batch)

清单为纯文本文件，每行一条任务：`外层|RAR[|输出]`，`#` 开头为注释行，空行忽略。
任一条任务失败不会中断后续任务，结束后汇总全部成败。

`tasks.txt` 示例:

```text
# 第三段输出可省略, 缺省时与外层同名
D:\media\v1.mp4|D:\rar\s1.rar|D:\out\v1.mp4
D:\media\v2.mp4|D:\rar\s2.rar
```

## 打包版 exe 的 CLI 注意事项

打包版 `PolyglotBuilder.exe` 使用 GUI 子系统 (`console=False`) 构建，双击无黑色控制台窗口，
代价是 **exe 的 CLI 模式没有控制台输出**。需要命令行输出时请:

- 改用源码运行: `python polyglot_build.py ...`；或
- 给 exe 加 `--log-file PATH` 参数，从日志文件查看处理过程。
