# 更新记录 (Changelog)

## 未发布 (Unreleased)

- **压缩外层视频大幅提速**: 不再写死 `-preset medium`, 改为按档位取 `VIDEO_PRESET` (high→faster / medium→veryfast / low→ultrafast)。实测 1080p30 60s → 720p 1.5Mbps: 179fps → 353fps (medium 档, **约 2 倍**), low 档达 681fps (**约 3.8 倍**), 输出体积几乎不变
- **音频智能直拷**: 探测源音轨, 已是 AAC 时用 `-c:a copy` (不重编码、不损音质), 无音轨时 `-an`, 其他编码才转 AAC 128k
- **进度显示预计剩余时间**: 预热 3 秒后进度消息附加 `(预计还需 X 分 Y 秒)`; 开始压缩时日志会告知实际使用的编码器
- **硬件编码改为可选**: 新增 CLI `--hw-encoder` 与 GUI “硬件编码”勾选 (仅勾选压缩后启用); 实测核显硬编 (h264_amf 316fps) 并不比多核 CPU 软编 (353fps) 快, 4K 源瓶颈在 CPU 解码+缩放, 故默认不开; 探测不到硬件时自动回退并在日志告知
- **fix: 硬件编码器探测假阴性**: 探测片段原为 64x64, AMF 会 `encoder->Init() failed` 导致“明明有硬件却回退 CPU”; 改为 640x360 + 3 帧 + 带码率后探测正确
- **fix: 探测函数不再拖垮主流程**: `_probe_audio_codec` / `detect_hw_encoder` / `_test_encode` 改为捕获任意异常并降级 (原本只捕 OSError/SubprocessError)
- **fix: 关窗时 Tk 报 `invalid command name ..._poll_log_queue`**: 日志轮询回调改为可取消 (`_poll_after_id` + `_stop_polling()`), 并绑定 `WM_DELETE_WINDOW` (关窗同时中止进行中的构建) 与 `<Destroy>`
- **其他**: 测试 120 → 145 用例; 文档同步 (docs/cli.md 新增“压缩速度”节与 `--hw-encoder`、docs/development.md 新增编码器约定)

## v1.1 (2026-09-03) — 资源台账

- **版本号**: 统一为 1.1 (`VERSION` 常量 / bat 窗口标题与横幅 / GUI 文件头 / README 徽章)
- **资源台账 (新功能)**: 新增 `polyglot_ledger.py`——记录资源名称、网盘平台与位置、分享链接、提取码、RAR 密码、备注; 文件名/大小/时间构建后自动预填
- **存储结构**: **`资源台账.json` 为唯一数据源** (原子写入: 临时文件 + `os.replace`), 同名的 **`.html` 查看页每次保存自动重生** (衍生物, 可删可重建)——相比单一 HTML 存储: 读取不再靠正则抠数据、不怕被编辑器误伤、查看器模板升级后旧数据自动用新界面渲染
- **旧版迁移**: 旧的单文件 HTML 台账自动迁移 (读数据 → 写 JSON → 旧 HTML 改名 `.bak` 保留 → 生成新查看页); GUI/CLI/位置记忆传 `.html` 路径也会自动规范化
- **台账查看器**: 浏览器直接打开, 支持搜索、按网盘筛选、密码遮罩(点"显示"才可见)、一键复制(密码/提取码/文件名)、导出带 BOM 的 CSV (Excel 中文不乱码)
- **GUI 入口**: 主界面新增「资源台账」按钮 (创建/打开); 构建成功后询问是否记账并弹出记账对话框; 台账位置记忆于 `ledger_config.json`
- **台账管理窗口**: `LedgerManagerDialog` 支持列表查看、实时搜索、**新增/编辑/删除** (双击行即编辑, 删除二次确认), 改动即时写回 JSON 并重生查看页; 网页版定位为只读查看器 (浏览器 `file://` 无写本地文件权限)
- **按钮布局与配色修复**: 「资源台账」从构建按钮行移至标题行右侧 (避开该行与文件卡片内按钮 24px vs 44px 的边距不一致导致的贴边感); 配色从中灰底白字 (#8E8E93, 对比不足) 改为浅蓝底蓝字 (#E8F1FD/#007AFF); 宽度 110→124 使文字不再拥挤
- **CLI 入口**: 新增 `--ledger PATH`、`--ledger-name`、`--ledger-netdisk`、`--ledger-location`、`--note`; 交互式终端下用 `getpass` 询问密码 (不回显); 未指定 `--ledger` 时无任何副作用
- **健壮性**: 台账读写失败抛 `LedgerError` 但不影响构建结果; JSON 中 `</` 转义为 `<\/` 防数据块提前闭合; 渲染用 `textContent` 防 HTML 注入
- **修复**: `RoundedButton._on_release` 不再硬编码主色, 自定义颜色按钮 (取消/台账) 点击后不再闪蓝
- **对话框修正**: 记账/编辑对话框的「文件名 / 大小 / 记录时间」由只读展示改为**可编辑输入框** (手动新增记录时不再无法填写文件名; 时间留空则自动填当前时间)
- **修复占位提示污染数据**: `PlaceholderEntry` 会把占位文字写进绑定的 `textvariable`, 导致未填写的字段被存成"可选"这类提示文字; 对话框改用普通输入框 + 常驻灰色提示, 主窗口新增 `_entry_value()` 把仅显示占位提示的输入框视为空 (未选文件时正确提示"请选择外层文件")
- **其他**: `.gitignore` 排除台账文件 (`.json`/`.html`/`.bak`) 与 `ledger_config.json` (含密码); mypy 纳入 `polyglot_ledger.py`; 测试 71 → 120 用例; 文档拆分新增 `docs/ledger.md`

## v1.0 (2026-09-02) — 首个公开发布版本

- **核心功能**: 将媒体/文档文件 (MP4/JPG/PDF/MP3/BMP) 与 AES-256 加密 RAR 拼接为"一文件两用"的 polyglot 文件——可正常播放/打开，改后缀为 `.zip` 又可解压
- **CLI**: `-y/--force` (非交互覆盖)、`--compress [high/medium/low]` (压缩外层视频, 需 ffmpeg)、`--batch MANIFEST` (批量构建)、`--log-file` (日志持久化)、`--deflate`、`--no-verify`、`--version`; Ctrl+C 干净取消并自动清理半成品
- **GUI**: tkinter 零依赖; 实时进度条与彩色分级日志、构建中可取消并恢复半成品、输出路径自定义、日志导出 `.txt`; 无参数运行 (含双击打包版 exe) 直达 GUI
- **大文件**: 8MB 分块流式处理, 内存占用恒定; 超过 4GB 自动 ZIP64 (数据描述符字段序已修复验证); ZIP 条目写入真实 DOS 时间戳 (取自 RAR mtime)
- **健壮性**: 构建后自动流式 CRC 校验 (大文件不卡 UI); 临时文件异常安全清理; ffmpeg 下载解压防 Zip Slip; 构建前预估输出体积与 RAR 魔数校验
- **质量与分发**: unittest 零依赖测试 (71 用例) + mypy 类型检查 + GitHub Actions CI (Windows/Ubuntu × Python 3.10/3.12 矩阵); PyInstaller onedir 打包 (`console=False`, 双击 exe 无黑色控制台闪烁)

> v1.0 之前的内部开发迭代 (v2.0~v3.0, 2026-07-26 同日完成) 已合并至本条目, 不再单独记录。
