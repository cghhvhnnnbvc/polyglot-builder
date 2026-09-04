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
| `polyglot_ledger.py` | 资源台账模块 (JSON 数据源 + HTML 查看页生成 + 旧版迁移) |
| `test_polyglot.py` | 回归测试 (unittest, 零依赖, 145 用例) |
| `polyglot_builder.spec` | PyInstaller 打包配置 (onedir, 可选) |
| `build_dist.bat` | 一键打包脚本 (装 PyInstaller → 跑测试 → 打包, 可选) |
| `.github/workflows/ci.yml` | GitHub Actions CI (mypy + unittest 多平台矩阵) |
| `.github/workflows/release.yml` | 自动发版 (推送 v* 标签即自动打包并发布到 Releases) |
| `docs/` | CLI 参数、资源台账、技术原理等详细文档 |
| `CHANGELOG.md` | 版本更新记录 |

## 运行测试与类型检查

```bash
python -m unittest test_polyglot          # 145 用例, 零依赖
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
  git tag v1.2 && git push origin v1.2
  ```

  流程: 校验标签与 `VERSION` 常量一致 → 跑测试 → PyInstaller 打包 → 压缩 zip → 创建 GitHub Release (自带中文说明)。

- **发版检查清单**: ① `VERSION` 常量 ② `polyglot_build.bat` 标题+2 处横幅 ③ `polyglot_gui.py` 文件头注释
  ④ `README.md` 版本徽章 ⑤ `CHANGELOG.md` 把"未发布"改为新版本条目 ⑥ `release.yml` 的 `name` 副标题与"本版本新增"段落。
  前三项不一致会被 `test_bat_versions_match_constant` 拦下; 发版前记得先改 `release.yml` 的本版亮点, 否则 Release 页会沿用上一版的说明。

- **版本号单一来源**: `polyglot_build.py` 的 `VERSION` 常量; `polyglot_build.bat` 与 `README` 徽章需同步, `test_version_format` 等用例守护 bat 与常量一致。

## 视频压缩的编码器约定

- **默认 CPU 编码**: `libx264` + `VIDEO_PRESET` (high→faster / medium→veryfast / low→ultrafast)。
  外层视频只是伪装道具, 低码率下 preset 带来的画质差异极小, 而速度差异达 2 倍以上
  (实测 medium preset 179fps → veryfast 353fps → ultrafast 681fps, 输出体积几乎相同)。
  **不要把 preset 改回 medium** —— 那是压缩耗时过长的主因。
- **硬件编码为可选** (`use_hw=True` / CLI `--hw-encoder` / GUI “硬件编码”勾选):
  实测核显硬编并不比多核 CPU 软编快 (h264_amf 316fps vs veryfast 353fps),
  4K 源瓶颈在 CPU 解码+缩放, 故不做默认; 价值在于弱 CPU 机型与低 CPU 占用。
- **探测必须实测**: `detect_hw_encoder()` 先看 `-encoders` 列表, 再用 `_test_encode()`
  真编几帧验证 (编码器被列出 ≠ 本机有硬件)。探测片段必须够大且带码率:
  64x64 会让 AMF `Init()` 失败造成假阴性 (已踩坑), 现用 640x360 + 3 帧 + `-b:v 1000k`。
- **所有探测函数一律降级不抛错** (`_probe_audio_codec` / `detect_hw_encoder` / `_test_encode`
  均 `except Exception`): 探测失败只能导致“少一个优化”, 绝不能让压缩主流程失败。
- **GUI 轮询回调可取消**: `_poll_after_id` + `_stop_polling()`, 并绑定 `<Destroy>` 与
  `WM_DELETE_WINDOW`; 否则销毁窗口时 Tk 会刷 `invalid command name ..._poll_log_queue`,
  污染测试与 CI 日志。

## 资源台账模块约定

- **JSON 是唯一数据源** (`资源台账.json`, 结构 `{"version": 1, "records": [...]}`);
  写入必须原子 (临时文件 + `os.replace`), 避免写一半损坏
- **HTML 查看页是衍生物** (同 stem 的 `.html`): 每次 `save_records` 由 JSON 重新生成,
  可随时删除重建; 数据内嵌而非外链 (浏览器拦截 `file://` 页面 fetch 同目录文件)
- 所有公开函数接受 `.json` 或旧版 `.html` 路径, 内部统一经 `normalize_ledger_path()` 规范化;
  `migrate_legacy_html()` 负责旧版单文件台账迁移 (旧 HTML 改名 `.bak` 保留)
- 生成查看页时必须把 JSON 中的 `</` 转义为 `<\/`, 否则值里的 `</script>` 会提前闭合数据块
  (`test_script_close_tag_escaped_in_view_only` 守护: 查看页转义、JSON 数据源保持原样)
- 查看器渲染一律用 `textContent` 而非 `innerHTML`, 避免用户输入造成 HTML 注入
- 读写失败统一抛 `LedgerError`; 记账失败**不得**影响构建结果 (CLI 仅警告)
- **增删改只在 GUI 管理窗口 (`LedgerManagerDialog`) 做**: 浏览器 `file://` 页面无写本地文件权限
  (File System Access API 不可用, Firefox/Safari 不支持), 网页版定位为只读查看器
- 台账文件 (`.json`/`.html`/`.bak`) 与 `ledger_config.json` 已在 `.gitignore` 中排除 (含密码, 绝不入库);
  新增测试时勿向仓库目录写台账配置 (参考 `TestLedgerCLI.setUp` 的 mock)
