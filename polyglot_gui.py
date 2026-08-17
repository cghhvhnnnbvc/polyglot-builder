# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Polyglot Builder - 图形界面 v3.0

现代极简设计 (VS Code / Notion 风格):
  - 扁平化设计，去除所有立体边框
  - Canvas 实现真正的圆角按钮
  - 自适应流式布局，输入框填满整行
  - 终端风格深色日志区
  - 配色: 背景 #F5F5F7 / 卡片 #FFFFFF / 主按钮 #007AFF
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import sys
import time
import queue
import struct
from typing import Optional

try:
    from polyglot_build import (build_polyglot, verify_polyglot, format_size,
                                COMP_STORED, COMP_DEFLATE, VERSION,
                                BuildCancelled, compress_video, find_ffmpeg,
                                download_ffmpeg, FFMPEG_MIRRORS,
                                VIDEO_QUALITY, DEFAULT_VIDEO_QUALITY)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from polyglot_build import (build_polyglot, verify_polyglot, format_size,
                                COMP_STORED, COMP_DEFLATE, VERSION,
                                BuildCancelled, compress_video, find_ffmpeg,
                                download_ffmpeg, FFMPEG_MIRRORS,
                                VIDEO_QUALITY, DEFAULT_VIDEO_QUALITY)


# ============================================================
# 文件拖拽支持 (Windows WM_DROPFILES, 零外部依赖)
# ============================================================
# 仅 Windows 原生支持; 其他平台静默降级为无拖拽。
_DROP_ENABLED = False
try:
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        _GWL_WNDPROC = -4
        _WM_DROPFILES = 0x0233
        _DragAcceptFiles = ctypes.windll.shell32.DragAcceptFiles
        _DragQueryFileW = ctypes.windll.shell32.DragQueryFileW
        _DragFinish = ctypes.windll.shell32.DragFinish
        _CallWindowProcW = ctypes.windll.user32.CallWindowProcW
        _SetWindowLongW = ctypes.windll.user32.SetWindowLongW
        if hasattr(wintypes, 'LONG_PTR'):
            _WNDPROC_TYPE = ctypes.WINFUNCTYPE(
                ctypes.c_int, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM)
        else:
            _WNDPROC_TYPE = ctypes.WINFUNCTYPE(
                ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_void_p)
        _DROP_ENABLED = True
except Exception:
    _DROP_ENABLED = False


def _enable_drop(widget, on_drop):
    """为 tkinter widget 启用文件拖拽。

    on_drop(paths: list[str]) -> None 在拖入文件释放时被调用,
    paths 为去重后的绝对路径列表 (保留释放顺序)。
    仅 Windows 生效; 其他平台为 no-op。
    """
    if not _DROP_ENABLED:
        return
    widget.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())

    # 保留旧 wndproc 引用, 避免被 GC
    state = {'prev': None}

    def _new_wndproc(hwnd, msg, wparam, lparam):
        if msg == _WM_DROPFILES:
            hdrop = wparam
            # 查询文件数
            count = _DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            paths = []
            for i in range(count):
                length = _DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                _DragQueryFileW(hdrop, i, buf, length + 1)
                p = buf.value
                if p and p not in paths:
                    paths.append(p)
            _DragFinish(hdrop)
            if paths:
                # 在 Tk 主线程安全地回调
                widget.after(0, lambda: on_drop(paths))
            return 0
        return _CallWindowProcW(state['prev'], hwnd, msg, wparam, lparam)

    proc = _WNDPROC_TYPE(_new_wndproc)
    state['prev'] = _SetWindowLongW(hwnd, _GWL_WNDPROC, proc)
    # 关键: 把 proc 绑定到 widget 防止 GC
    widget._drop_proc = proc
    _DragAcceptFiles(hwnd, True)


# ============================================================
# 配色方案
# ============================================================
C_BG           = '#F5F5F7'   # 主背景
C_CARD         = '#FFFFFF'   # 卡片背景
C_PRIMARY      = '#007AFF'   # 主按钮蓝
C_PRIMARY_H    = '#0062CC'   # 悬停蓝
C_PRIMARY_A    = '#004C99'   # 按下蓝
C_DANGER       = '#FF3B30'   # 取消/危险按钮红
C_DANGER_H     = '#D93228'   # 悬停红
C_DANGER_A     = '#B52A21'   # 按下红
C_DISABLED     = '#C7C7CC'   # 禁用灰
C_TEXT         = '#1D1D1F'   # 主文本
C_TEXT_SEC     = '#86868B'   # 次文本
C_BORDER       = '#D2D2D7'   # 边框
C_BAR_BG      = '#E5E5EA'   # 进度条底色
C_BAR_FILL    = '#34C759'   # 进度条绿色
C_LOG_BG      = '#F0F0F2'   # 日志背景 (浅色系)
C_LOG_FG      = '#3A3A3C'   # 日志默认文字

# ============================================================
# 字体常量 (跨平台回退)
# ============================================================
# 优先使用 Segoe UI / Consolas (Windows), 其他平台自动回退到系统默认字体。
# 字体族在运行时经 _resolve_fonts() 检测后覆写, 见 PolyglotGUI.__init__。
_FONT_UI    = 'Segoe UI'      # 界面字体
_FONT_MONO  = 'Consolas'      # 等宽字体
FONT_TITLE   = (_FONT_UI + ' Semibold', 17)
FONT_SECTION = (_FONT_UI + ' Semibold', 10)
FONT_LABEL   = (_FONT_UI, 9)
FONT_ENTRY   = (_FONT_UI, 10)
FONT_BTN     = (_FONT_UI + ' Semibold', 11)
FONT_BROWSE  = (_FONT_UI, 9)
FONT_MONO    = (_FONT_MONO, 10)
FONT_HINT    = (_FONT_UI, 9)
FONT_STATUS  = (_FONT_MONO, 9)


def _resolve_fonts(root):
    """根据当前平台可用字体覆写全局 FONT_* 常量。

    在 Tk 根窗口创建后调用。若 Segoe UI / Consolas 不存在
    (如 Linux/macOS), 回退到系统默认字体族 (TkDefaultFont 族)。
    """
    global FONT_TITLE, FONT_SECTION, FONT_LABEL, FONT_ENTRY, FONT_BTN
    global FONT_BROWSE, FONT_MONO, FONT_HINT, FONT_STATUS

    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
    except Exception:
        return  # 无法检测, 保留默认

    ui_family = _FONT_UI if _FONT_UI in families else 'TkDefaultFont'
    mono_family = _FONT_MONO if _FONT_MONO in families else 'TkFixedFont'

    FONT_TITLE   = (ui_family + ' Semibold', 17)
    FONT_SECTION = (ui_family + ' Semibold', 10)
    FONT_LABEL   = (ui_family, 9)
    FONT_ENTRY   = (ui_family, 10)
    FONT_BTN     = (ui_family + ' Semibold', 11)
    FONT_BROWSE  = (ui_family, 9)
    FONT_MONO    = (mono_family, 10)
    FONT_HINT    = (ui_family, 9)
    FONT_STATUS  = (mono_family, 9)


# ============================================================
# 自定义圆角按钮 (Canvas)
# ============================================================
class RoundedButton(tk.Canvas):
    """Canvas 实现的圆角按钮，支持悬停/按下/禁用状态"""

    def __init__(self, parent, text, command=None, *,
                 bg=C_PRIMARY, fg='white', radius=5,
                 hover_bg=None, active_bg=None,
                 font=FONT_BTN, canvas_bg=None, **kw):
        self._canvas_bg = canvas_bg or C_BG
        kw.setdefault('height', 42)
        super().__init__(parent, highlightthickness=0, bg=self._canvas_bg, **kw)
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or C_PRIMARY_H
        self._active_bg = active_bg or C_PRIMARY_A
        self._text = text
        self._radius = radius
        self._font = font
        self._enabled = True

        self.bind('<ButtonPress-1>', self._on_press)
        self.bind('<ButtonRelease-1>', self._on_release)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<Configure>', self._on_resize)

    def _on_enter(self, e):
        if self._enabled:
            self._draw(self._hover_bg)

    def _on_leave(self, e):
        if self._enabled:
            self._draw(self._bg)

    def _on_press(self, e):
        if self._enabled:
            self._draw(self._active_bg)

    def _on_release(self, e):
        if self._enabled:
            self._draw(C_PRIMARY_H)
            if self._cmd:
                self._cmd()

    def _on_resize(self, e):
        color = C_DISABLED if not self._enabled else self._bg
        self._draw(color)

    def configure(self, **kw):  # type: ignore[override]
        # 重写签名以支持自定义 state/command 处理; 与 tkinter.Canvas 签名不同
        if 'state' in kw:
            self._enabled = kw['state'] != tk.DISABLED
            color = C_DISABLED if not self._enabled else self._bg
            self._draw(color)
        if 'command' in kw:
            self._cmd = kw['command']

    def _draw(self, bg_color):
        self.delete('all')
        w = self.winfo_width()
        if w < 10:
            w = max(self.winfo_reqwidth(), 200)
        h = self.winfo_height()
        if h < 5:
            h = 42
        r = self._radius

        # 圆角矩形
        pts = [
            r, 0, w - r, 0,
            w, 0, w, r,
            w, h - r, w, h,
            w - r, h, r, h,
            0, h, 0, h - r,
            0, r, 0, 0,
        ]
        self.create_polygon(pts, fill=bg_color, outline='', smooth=True)

        # 居中文字
        self.create_text(
            w // 2, h // 2,
            text=self._text, fill=self._fg, font=self._font
        )


# ============================================================
# 占位符输入框
# ============================================================
class PlaceholderEntry(ttk.Entry):
    """带灰色占位符提示的输入框，获焦时自动清除占位文字。

    维护 _auto_filled 标志以区分"程序自动同步设置"与"用户主动输入",
    供外层路径自动跟随逻辑判断: 自动填充的值可在后续被覆盖,
    用户主动输入的值不再被覆盖。
    """

    def __init__(self, parent, placeholder='', **kw):
        self._placeholder = placeholder
        self._showing_ph = True
        self._auto_filled = False
        self._ph_fg = '#A0A0A5'
        self._fg = kw.pop('foreground', C_TEXT)
        super().__init__(parent, **kw)

        if placeholder:
            self.configure(foreground=self._ph_fg)
            self.insert(0, placeholder)

        self.bind('<FocusIn>', self._focus_in)
        self.bind('<FocusOut>', self._focus_out)
        self.bind('<Key>', self._on_user_key)

    def _on_user_key(self, _):
        # 用户键入即视为非自动填充
        self._auto_filled = False

    def _focus_in(self, _):
        if self._showing_ph:
            self.delete(0, tk.END)
            self.configure(foreground=self._fg)
            self._showing_ph = False
            self._auto_filled = False

    def _focus_out(self, _):
        if not self.get():
            if self._placeholder:
                self.insert(0, self._placeholder)
                self.configure(foreground=self._ph_fg)
                self._showing_ph = True
                self._auto_filled = False

    def set(self, value):
        """程序设置值时视为非自动 (如浏览按钮结果)。"""
        self.delete(0, tk.END)
        if value:
            self.configure(foreground=self._fg)
            self._showing_ph = False
            self._auto_filled = False
            self.insert(0, value)
            self.xview_moveto(1)

    def set_auto(self, value):
        """自动同步设置值 (供外层路径自动跟随); 标记为可被后续覆盖。"""
        self.delete(0, tk.END)
        if value:
            self.configure(foreground=self._fg)
            self._showing_ph = False
            self._auto_filled = True
            self.insert(0, value)
            self.xview_moveto(1)

    def get(self):
        if self._showing_ph:
            return ''
        return super().get()

    def real_get(self):
        """返回原始文本 (含占位符)"""
        return ttk.Entry.get(self)

    def clear(self):
        """清空内容，恢复占位符"""
        self.delete(0, tk.END)
        if self._placeholder:
            self.insert(0, self._placeholder)
            self.configure(foreground=self._ph_fg)
            self._showing_ph = True
            self._auto_filled = False

    @property
    def has_value(self):
        return not self._showing_ph and bool(self.get())

    @property
    def is_auto_filled(self):
        return self._auto_filled


# ============================================================
# 输出文件保存选项 (根据外层扩展名动态生成)
# ============================================================
def get_output_save_options(outer_path: str) -> tuple[list[tuple[str, str]], str]:
    """根据外层文件扩展名返回保存对话框的 (filetypes, defaultextension)。

    当外层是受支持的多格式格式时, 将对应类型放在最前并设为默认扩展;
    否则列出全部受支持类型, 默认 .mp4。
    """
    ext = os.path.splitext(outer_path)[1].lower() if outer_path else ''

    # (描述, 模式). 命中项会被前置, 索引见 type_map
    all_types = [
        ('MP4 视频 (*.mp4)',        '*.mp4'),
        ('JPEG 图片 (*.jpg *.jpeg)', '*.jpg *.jpeg'),
        ('PNG 图片 (*.png)',        '*.png'),
        ('BMP 图片 (*.bmp)',        '*.bmp'),
        ('PDF 文档 (*.pdf)',        '*.pdf'),
        ('MP3 音频 (*.mp3)',        '*.mp3'),
        ('FLAC 音频 (*.flac)',      '*.flac'),
        ('WAV 音频 (*.wav)',        '*.wav'),
        ('MKV 视频 (*.mkv)',        '*.mkv'),
        ('AVI 视频 (*.avi)',        '*.avi'),
    ]
    type_map = {
        '.mp4': 0, '.jpg': 1, '.jpeg': 1, '.png': 2, '.bmp': 3,
        '.pdf': 4, '.mp3': 5, '.flac': 6, '.wav': 7,
        '.mkv': 8, '.avi': 9,
    }
    idx = type_map.get(ext)
    filetypes = []
    if idx is not None:
        filetypes.append(all_types[idx])
        for i, t in enumerate(all_types):
            if i != idx:
                filetypes.append(t)
        default_ext = ext
    else:
        filetypes = list(all_types)
        default_ext = '.mp4'
    filetypes.append(('所有文件 (*.*)', '*.*'))
    return filetypes, default_ext


def _should_follow_outer(outer: str, has_value: bool, is_auto_filled: bool) -> bool:
    """判断输出条目是否应自动跟随外层路径同步。

    当外层有值, 且输出条目没有"用户主动输入过的值"
    (即当前为空, 或当前值为自动同步填充), 返回 True。
    """
    return bool(outer) and (not has_value or is_auto_filled)


# ============================================================
# 主界面
# ============================================================
class PolyglotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('Polyglot Builder')
        self.root.geometry('880x700')
        self.root.minsize(760, 600)
        self.root.configure(bg=C_BG)

        self.build_thread: Optional[threading.Thread] = None
        self.log_queue: queue.Queue[tuple] = queue.Queue()
        self._stop_event = threading.Event()

        # 检测并应用跨平台字体回退 (须在创建任何控件前)
        _resolve_fonts(root)

        self._setup_styles()
        self._create_widgets()
        self._poll_log_queue()

        # 启用文件拖拽到主窗口 (Windows; 其他平台静默降级)
        _enable_drop(self.root, self._on_drop)

    # --------------------------------------------------------
    # 表面视频压缩
    # --------------------------------------------------------
    def _on_compress_toggle(self):
        """勾选压缩时启用质量档位下拉; 取消时禁用。

        若未检测到 ffmpeg, 引导用户联网下载 (或选择取消)。
        """
        enabled = self._compress_var.get()
        if not enabled:
            self._quality_combo.configure(state='disabled')
            return

        self._quality_combo.configure(state='readonly')
        if find_ffmpeg():
            return  # 已有 ffmpeg, 直接用

        # 无 ffmpeg: 引导下载
        self._prompt_download_ffmpeg()

    def _prompt_download_ffmpeg(self):
        """未检测到 ffmpeg 时, 弹出提示框让用户选择下载/取消。"""
        # 镜像选项文本
        mirror_names = [name for name, _url in FFMPEG_MIRRORS]
        result = messagebox.askquestion(
            '需要 ffmpeg',
            '未检测到 ffmpeg。\n\n'
            '压缩表面视频需要 ffmpeg 组件。\n'
            '是否从网络下载并自动安装？\n\n'
            f'默认镜像: {mirror_names[0]}\n'
            '(如下载缓慢可切换国内镜像)\n\n'
            '点击"否"将取消压缩, 但可继续普通构建。',
            icon='question')
        if result != 'yes':
            self._compress_var.set(False)
            self._quality_combo.configure(state='disabled')
            return

        # 确认下载后, 后台线程下载, 避免阻塞 UI
        self._download_ffmpeg_async(mirror_index=0)

    def _download_ffmpeg_async(self, mirror_index: int = 0):
        """在后台线程下载 ffmpeg; 成功/失败后回到主线程处理。"""
        self._log_async(f'开始下载 ffmpeg (镜像: {FFMPEG_MIRRORS[mirror_index][0]})...',
                        'info')
        self.cancel_btn.configure(state=tk.NORMAL)

        def dl():
            try:
                download_ffmpeg(mirror_index=mirror_index,
                                callback=self._ffmpeg_dl_cb)
                self.root.after(0, self._on_ffmpeg_dl_success)
            except BuildCancelled:
                self.root.after(0, self._on_ffmpeg_dl_cancel)
            except Exception as e:
                self.root.after(0, self._on_ffmpeg_dl_error, str(e))

        threading.Thread(target=dl, daemon=True).start()

    def _ffmpeg_dl_cb(self, phase, cur, total, msg):
        if phase == 'info':
            self._log_async(msg, 'info')
        elif phase == 'download':
            pct = cur * 100 // total if total > 0 else 0
            self.root.after(0, self._set_progress, pct)
            self.root.after(0, self._set_status, f'下载 ffmpeg... {pct}%')

    def _on_ffmpeg_dl_success(self):
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('ffmpeg 已就绪')
        self._set_progress(100)
        self._log_async('ffmpeg 下载并安装完成, 可使用压缩功能。', 'success')
        messagebox.showinfo('ffmpeg 已安装',
                            'ffmpeg 下载并安装完成。\n现在可以使用表面视频压缩功能。')

    def _on_ffmpeg_dl_cancel(self):
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('已取消下载')
        self._set_progress(0)
        self._compress_var.set(False)
        self._quality_combo.configure(state='disabled')
        self._log_async('ffmpeg 下载已取消。', 'warning')

    def _on_ffmpeg_dl_error(self, msg):
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('下载失败')
        self._set_progress(0)
        self._compress_var.set(False)
        self._quality_combo.configure(state='disabled')
        self._log_async(f'ffmpeg 下载失败: {msg}', 'error')
        # 提示切换镜像或手动安装
        retry = messagebox.askyesno(
            '下载失败',
            f'ffmpeg 下载失败:\n{msg}\n\n'
            '是否尝试其他镜像 (国内镜像可能更快)？')
        if retry:
            # 依次尝试所有镜像直到成功
            self._download_ffmpeg_all()

    def _download_ffmpeg_all(self):
        """依次尝试所有镜像, 直到某个成功或全部失败。"""
        self._log_async('依次尝试所有镜像下载 ffmpeg...', 'info')

        def dl_all():
            last_err = '未知错误'
            for i, (name, _url) in enumerate(FFMPEG_MIRRORS):
                self.root.after(0, self._log_async, f'尝试镜像 [{name}]...', 'info')
                try:
                    download_ffmpeg(mirror_index=i,
                                    callback=self._ffmpeg_dl_cb)
                    self.root.after(0, self._on_ffmpeg_dl_success)
                    return
                except BuildCancelled:
                    self.root.after(0, self._on_ffmpeg_dl_cancel)
                    return
                except Exception as e:
                    last_err = str(e)
                    continue
            # 全部镜像失败: 静默报错 (不再弹重试框, 避免循环)
            self._finalize_download_failure(last_err)

        threading.Thread(target=dl_all, daemon=True).start()

    def _finalize_download_failure(self, msg):
        """下载彻底失败: 恢复 UI, 记录错误并提示手动安装 (不弹重试框)。"""
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('下载失败')
        self._set_progress(0)
        self._compress_var.set(False)
        self._quality_combo.configure(state='disabled')
        self._log_async(f'ffmpeg 下载失败: {msg}', 'error')
        messagebox.showwarning(
            'ffmpeg 下载失败',
            f'所有镜像均下载失败:\n{msg}\n\n'
            '请手动安装 ffmpeg:\n'
            '  1. 从 https://ffmpeg.org/download.html 下载\n'
            '  2. 将 ffmpeg.exe 放入程序目录的 ffmpeg/ 文件夹\n'
            '  3. 或安装并加入系统 PATH')

    def _selected_quality(self) -> Optional[str]:
        """读取当前选中的质量档位 key (如 'high'/'medium'/'low'), 无效返回 None。"""
        label = self._quality_combo.get()
        for k in VIDEO_QUALITY:
            if label.startswith(f'{k} -'):
                return k
        return DEFAULT_VIDEO_QUALITY if self._compress_var.get() else None

    # --------------------------------------------------------
    # 拖拽处理
    # --------------------------------------------------------
    def _on_drop(self, paths):
        """拖入文件释放时回调: 按扩展名智能分配到外层/RAR 槽。

        - .rar -> 加密 RAR 槽
        - 其他 -> 外层文件槽 (若外层已填则填 RAR 槽)
        拖入多个时按顺序填充剩余空槽。
        """
        if not paths:
            return
        assigned_outer = assigned_rar = False
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            if ext == '.rar' and (not assigned_rar or not self._rar_path.get()):
                self._rar_path.set(p)
                assigned_rar = True
            elif not assigned_outer or not self._outer_path.get():
                self._outer_path.set(p)
                assigned_outer = True
            elif not assigned_rar or not self._rar_path.get():
                self._rar_path.set(p)
                assigned_rar = True
        # _outer_path trace 会自动同步输出路径

    # --------------------------------------------------------
    # 样式 (ttk)
    # --------------------------------------------------------
    def _setup_styles(self):
        self.style = ttk.Style()
        if 'clam' in self.style.theme_names():
            self.style.theme_use('clam')

        s = self.style
        # 基础
        s.configure('TFrame', background=C_BG)
        s.configure('TLabel', background=C_BG, font=FONT_LABEL, foreground=C_TEXT)
        # 输入框: 扁平、无边框、内嵌于卡片
        s.configure('TEntry', font=FONT_ENTRY, padding=(10, 7),
                     borderwidth=0, relief='flat', fieldbackground=C_CARD)
        # 浏览按钮
        s.configure('TButton', font=FONT_BROWSE, padding=(12, 4),
                     borderwidth=0, relief='flat')
        # 进度条
        s.configure('Horizontal.TProgressbar', thickness=10,
                     troughcolor=C_BAR_BG, background=C_BAR_FILL, borderwidth=0)

    # --------------------------------------------------------
    # 创建组件
    # --------------------------------------------------------
    def _create_widgets(self):
        main = ttk.Frame(self.root, padding=(24, 18, 24, 14))
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        # 行 0: 标题
        # 行 1: 文件选择卡片
        # 行 2: 选项 (压缩方式)
        # 行 3: 构建按钮
        # 行 4: 进度条
        # 行 5: 日志区 (唯一可拉伸)
        # 行 6: 底部提示
        main.rowconfigure(5, weight=1)

        # === 标题 ===
        title = ttk.Label(main, text='Polyglot Builder',
                          font=FONT_TITLE, foreground=C_TEXT)
        title.grid(row=0, column=0, sticky='w', pady=(0, 16))

        # === 文件选择卡片 ===
        card_frame = tk.Frame(main, bg=C_CARD, bd=1, relief='solid',
                              highlightbackground=C_BORDER, highlightthickness=1)
        card_frame.grid(row=1, column=0, sticky='ew', pady=(0, 14))

        card = ttk.Frame(card_frame, padding=(20, 16, 20, 16))
        card.pack(fill=tk.BOTH, expand=True)
        card.columnconfigure(1, weight=1)

        self._outer_path  = tk.StringVar()
        self._rar_path    = tk.StringVar()
        self._output_path = tk.StringVar()

        self._outer_entry = self._file_row(card, 0, '外层文件 *', self._outer_path,
                        '选择视频/图片/文档',
                        [('视频文件', '*.mp4 *.mkv *.avi'),
                         ('图片文件', '*.jpg *.jpeg *.png *.bmp'),
                         ('文档文件', '*.pdf'),
                         ('音频文件', '*.mp3 *.flac *.wav'),
                         ('所有文件', '*.*')])

        self._rar_entry = self._file_row(card, 1, '加密 RAR *', self._rar_path,
                        '选择 WinRAR 加密压缩包',
                        [('RAR 压缩包', '*.rar'), ('所有文件', '*.*')])

        self._out_entry = self._file_row(card, 2, '输出路径', self._output_path,
                        '留空则覆盖外层文件',
                        [('MP4 文件', '*.mp4'), ('所有文件', '*.*')],
                        save_dialog=True)

        # 外层文件变化时自动填充输出路径
        self._outer_path.trace_add('write', lambda *a: self._auto_output())

        # === 选项区 (Deflate 压缩 / 表面视频压缩 + 档位) ===
        # 垂直堆叠 (一上一下), 避免横向被窗口挤压/截断
        # (原并排方案在窗口稍窄时第二行被压到边界, 文本截断)
        opt_frame = ttk.Frame(main)
        opt_frame.grid(row=2, column=0, sticky='ew', pady=(0, 10))

        # 第 1 行: Deflate 压缩
        self._deflate_var = tk.BooleanVar(value=False)
        deflate_cb = ttk.Checkbutton(
            opt_frame, text='Deflate 压缩 (默认不压缩，RAR 已压缩无需再压)',
            variable=self._deflate_var
        )
        deflate_cb.pack(anchor='w', pady=(0, 4))

        # 第 2 行: 压缩复选框 + 质量档位下拉 (右对齐, 复选框占左侧)
        compress_row = ttk.Frame(opt_frame)
        compress_row.pack(anchor='w', fill='x')
        self._compress_var = tk.BooleanVar(value=False)
        compress_cb = ttk.Checkbutton(
            compress_row,
            text='压缩表面视频 (减小最终体积, 提高隐蔽性)',
            variable=self._compress_var, command=self._on_compress_toggle
        )
        compress_cb.pack(side=tk.LEFT, padx=(0, 12))

        # 质量档位下拉 (仅勾选时可用)
        self._quality_var = tk.StringVar(value=DEFAULT_VIDEO_QUALITY)
        quality_labels = [f'{k} - {VIDEO_QUALITY[k][2]}' for k in VIDEO_QUALITY]
        self._quality_combo = ttk.Combobox(
            compress_row, state='disabled', width=22,
            textvariable=self._quality_var, values=quality_labels
        )
        self._quality_combo.pack(side=tk.LEFT)
        self._quality_combo.set(quality_labels[0])
        self._quality_labels = quality_labels

        # === 构建按钮 (Canvas 圆角) ===
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=3, column=0, sticky='ew', pady=(0, 14))
        btn_frame.columnconfigure(0, weight=1)

        self.build_btn = RoundedButton(
            btn_frame, text='开始构建', command=self._start_build, canvas_bg=C_BG
        )
        self.build_btn.grid(row=0, column=0, sticky='ew')

        self.cancel_btn = RoundedButton(
            btn_frame, text='取消', command=self._cancel_build,
            bg=C_DANGER, hover_bg=C_DANGER_H, active_bg=C_DANGER_A,
            canvas_bg=C_BG, width=80
        )
        self.cancel_btn.grid(row=0, column=1, sticky='ew', padx=(10, 0))
        self.cancel_btn.configure(state=tk.DISABLED)

        # === 进度条 ===
        prog_frame = ttk.Frame(main)
        prog_frame.grid(row=4, column=0, sticky='ew', pady=(0, 12))
        prog_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(
            prog_frame, mode='determinate', style='Horizontal.TProgressbar'
        )
        self.progress.grid(row=0, column=0, sticky='ew')

        self.progress_lbl = ttk.Label(
            prog_frame, text='就绪', font=FONT_STATUS, foreground=C_TEXT_SEC
        )
        self.progress_lbl.grid(row=0, column=1, padx=(12, 0))

        # === 日志区 (浅色输出面板) ===
        log_outer = tk.Frame(main, bg=C_CARD, bd=1, relief='solid',
                             highlightbackground=C_BORDER, highlightthickness=1)
        log_outer.grid(row=5, column=0, sticky='nsew', pady=(0, 12))
        log_outer.columnconfigure(0, weight=1)
        log_outer.rowconfigure(1, weight=1)

        # 日志标题栏
        log_bar = tk.Frame(log_outer, bg='#E8E8ED', height=28)
        log_bar.grid(row=0, column=0, columnspan=2, sticky='ew')
        log_bar.grid_propagate(False)
        tk.Label(log_bar, text='  输出日志',
                 font=(FONT_LABEL[0], 9, 'bold'),
                 bg='#E8E8ED', fg=C_TEXT_SEC, anchor='w').pack(
            side=tk.LEFT, padx=(8, 0), pady=(4, 0)
        )

        # 日志文本
        self.log = tk.Text(
            log_outer, font=FONT_MONO,
            bg=C_LOG_BG, fg=C_LOG_FG,
            insertbackground=C_LOG_FG,
            relief='flat', bd=0,
            padx=12, pady=8, height=6,
            wrap=tk.WORD, state=tk.DISABLED, undo=False
        )
        log_scroll = tk.Scrollbar(log_outer, orient=tk.VERTICAL,
                                  command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)

        self.log.grid(row=1, column=0, sticky='nsew')
        log_scroll.grid(row=1, column=1, sticky='ns')

        self.log.tag_configure('info',    foreground=C_LOG_FG)
        self.log.tag_configure('success', foreground='#30A14E')
        self.log.tag_configure('warning', foreground='#9A6700')
        self.log.tag_configure('error',   foreground='#E5484D')

        # 初始欢迎信息
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f'$ Polyglot Builder v{VERSION}\n', 'success')
        self.log.insert(tk.END, '$ 选择文件后点击「开始构建」\n', 'info')
        self.log.configure(state=tk.DISABLED)

        # === 底部提示 ===
        hint = ttk.Label(
            main,
            text='改后缀 .zip  →  WinRAR 打开  →  输入 RAR 密码  →  得到内容',
            font=FONT_HINT, foreground=C_TEXT_SEC, anchor=tk.CENTER
        )
        hint.grid(row=6, column=0, sticky='ew')

    # --------------------------------------------------------
    # 文件选择行
    # --------------------------------------------------------
    def _file_row(self, parent, row, label_text, var, placeholder, filetypes,
                  optional=False, save_dialog=False):
        lbl = ttk.Label(parent, text=label_text, width=10, anchor='e')
        lbl.grid(row=row, column=0, sticky='e', padx=(0, 10), pady=6)

        entry = PlaceholderEntry(parent, placeholder=placeholder,
                                 textvariable=var, font=FONT_ENTRY)
        entry.grid(row=row, column=1, sticky='ew', pady=6)

        btn_text = '另存为' if save_dialog else '浏览'
        btn = ttk.Button(parent, text=btn_text, width=7)
        btn.grid(row=row, column=2, padx=(8, 0), pady=6)
        btn.configure(command=lambda: self._browse(var, filetypes, entry, save_dialog))
        return entry

    def _auto_output(self):
        outer = self._outer_path.get()
        out_entry = getattr(self, '_out_entry', None)
        if out_entry is None:
            return
        if _should_follow_outer(outer, out_entry.has_value,
                                out_entry.is_auto_filled):
            self._output_path.set(outer)
            out_entry.set_auto(outer)
        elif (not outer
              and (not out_entry.has_value or out_entry.is_auto_filled)):
            out_entry.clear()

    def _browse(self, var, filetypes, entry=None, save_dialog=False):
        if save_dialog:
            # 输出行: 根据当前外层扩展名动态提供匹配的文件类型与默认扩展
            _filetypes, _default_ext = get_output_save_options(
                self._outer_path.get())
            path = filedialog.asksaveasfilename(
                title='选择保存位置和文件名',
                filetypes=_filetypes, defaultextension=_default_ext
            )
        else:
            path = filedialog.askopenfilename(title='选择文件', filetypes=filetypes)
        if path:
            if isinstance(entry, PlaceholderEntry):
                entry.set(path)
            var.set(path)
            if entry:
                entry.xview_moveto(1)

    # --------------------------------------------------------
    # 日志 (线程安全)
    # --------------------------------------------------------
    def _log(self, message, level='info'):
        self.log.configure(state=tk.NORMAL)
        ts = time.strftime('%H:%M:%S')
        self.log.insert(tk.END, f'[{ts}] {message}\n', level)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _log_async(self, message, level='info'):
        self.log_queue.put((message, level))

    def _poll_log_queue(self):
        try:
            while True:
                self._log(*self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll_log_queue)

    # --------------------------------------------------------
    # 进度
    # --------------------------------------------------------
    def _set_progress(self, pct):
        self.progress.configure(value=pct)

    def _set_status(self, text):
        self.progress_lbl.configure(text=text)

    # --------------------------------------------------------
    # 构建
    # --------------------------------------------------------
    def _start_build(self):
        outer = self._outer_path.get().strip()
        rar   = self._rar_path.get().strip()
        out   = self._output_path.get().strip()

        if not outer:
            messagebox.showwarning('提示', '请选择外层文件'); return
        if not rar:
            messagebox.showwarning('提示', '请选择加密 RAR 文件'); return
        if not os.path.isfile(outer):
            messagebox.showerror('错误', f'外层文件不存在:\n{outer}'); return
        if not os.path.isfile(rar):
            messagebox.showerror('错误', f'RAR 文件不存在:\n{rar}'); return

        if not out:
            out = outer

        out_dir = os.path.dirname(out)
        if out_dir and not os.path.isdir(out_dir):
            messagebox.showerror('错误', f'输出目录不存在:\n{out_dir}'); return
        if os.path.exists(out):
            if not messagebox.askyesno('确认覆盖', f'将覆盖已有文件:\n\n{out}'):
                return

        # 锁定 UI
        self.build_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.progress['value'] = 0
        self._set_status('准备中...')
        self.log.configure(state=tk.NORMAL)
        self.log.delete(1.0, tk.END)
        self.log.configure(state=tk.DISABLED)

        self._stop_event.clear()
        self.build_thread = threading.Thread(
            target=self._run, args=(outer, rar, out), daemon=True
        )
        self.build_thread.start()

    def _cancel_build(self):
        self._stop_event.set()
        self.cancel_btn.configure(state=tk.DISABLED)
        self._log_async('正在取消构建...', 'warning')

    def _run(self, outer, rar, out):
        method = COMP_DEFLATE if self._deflate_var.get() else COMP_STORED
        quality = self._selected_quality()
        compressed_outer = None

        def cb(phase, cur, total, msg):
            if phase in ('start', 'info'):
                self._log_async(msg, 'info')
            elif phase in ('compress', 'copy', 'verify') and total > 0:
                pct = cur * 100 // total
                self.root.after(0, self._set_progress, pct)
                self.root.after(0, self._set_status, msg)
            elif phase == 'done':
                self.root.after(0, self._set_progress, 100)
                self.root.after(0, self._set_status, '完成')
                self._log_async(msg, 'success')

        try:
            # 若勾选压缩: 先用 ffmpeg 压缩外层视频, 再用压缩产物拼接
            effective_outer = outer
            if quality:
                # 校验外层是视频
                if not self._is_video_ext(outer):
                    raise ValueError(
                        '压缩表面视频仅支持视频外层文件 (mp4/mkv/avi/mov/webm 等)。\n'
                        '请更换外层为视频, 或取消勾选压缩。')
                # 压缩到临时路径
                compressed_outer = self._temp_compressed_path(outer)
                compress_video(outer, compressed_outer, quality=quality,
                               callback=cb, stop_event=self._stop_event)
                effective_outer = compressed_outer

            build_polyglot(effective_outer, rar, out, callback=cb,
                           method=method, stop_event=self._stop_event)
            verify_polyglot(out, callback=cb)
            self.root.after(0, self._on_success, out)
        except BuildCancelled as e:
            self._log_async(f'构建已取消: {e}', 'warning')
            self.root.after(0, self._on_cancel)
        except Exception as e:
            self._log_async(f'构建失败: {e}', 'error')
            self.root.after(0, self._on_error, str(e))
        finally:
            # 清理压缩外层临时文件
            if compressed_outer and os.path.exists(compressed_outer):
                try:
                    os.remove(compressed_outer)
                except OSError:
                    pass

    @staticmethod
    def _is_video_ext(path: str) -> bool:
        video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv'}
        return os.path.splitext(path)[1].lower() in video_exts

    @staticmethod
    def _temp_compressed_path(outer: str) -> str:
        """为压缩外层生成临时路径 (同目录, 前缀 polyglot_compressed_)。"""
        d = os.path.dirname(os.path.abspath(outer))
        base = os.path.splitext(os.path.basename(outer))[0]
        return os.path.join(d, f'polyglot_compressed_{base}.mp4')

    def _on_success(self, out):
        self.build_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        try:
            size_str = format_size(os.path.getsize(out))
        except OSError:
            size_str = '未知'
        messagebox.showinfo(
            '构建完成',
            f'构建成功!\n\n'
            f'文件: {os.path.basename(out)}\n'
            f'大小: {size_str}\n'
            f'路径: {out}\n\n'
            f'使用方式:\n'
            f'  1. 直接打开 -> 播放/查看外层内容\n'
            f'  2. 改后缀 .zip -> WinRAR 解压\n'
            f'  3. 输入 RAR 密码 -> 得到最终内容'
        )

    def _on_error(self, msg):
        self.build_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('失败')
        messagebox.showerror('构建失败', f'构建出错:\n\n{msg}')

    def _on_cancel(self):
        self.build_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('已取消')
        self._set_progress(0)


# ============================================================
# 启动入口
# ============================================================
def launch_gui():
    # DPI 感知 (高分辨率屏)，必须在 tk.Tk() 之前调用
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    _set_window_icon(root)
    PolyglotGUI(root)
    root.mainloop()


def _draw_icon_pixels(img, size):
    """在 tk.PhotoImage img (size x size) 上绘制应用图标。

    设计: 深蓝渐变圆角底 + 三层错位白色拼接块 (表达多格式层叠拼接)
          + 右下角黄色迷你锁 (表达内层 AES-256 加密)。
    """
    # 颜色 (RGB)
    BG_TOP = (0x1E, 0x88, 0xE5)      # 浅蓝
    BG_BOT = (0x0D, 0x47, 0xA1)      # 深蓝
    WHITE = (0xFF, 0xFF, 0xFF)
    LAYER_MID = (0xBD, 0xDE, 0xFB)   # 顶层媒体占位淡蓝
    LOCK = (0xFF, 0xC1, 0x07)        # 金黄
    LOCK_DARK = (0x0D, 0x47, 0xA1)   # 锁孔深蓝

    def hexcolor(rgb):
        return '#%02X%02X%02X' % rgb

    def bg_at(y):
        t = y / size
        return (int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t),
                int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t),
                int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t))

    def blend(fg, bg, a):
        return tuple(int(fg[i] * a + bg[i] * (1 - a)) for i in range(3))

    # 圆角方块外框半径
    R = size * 0.18

    def inside_rounded(x, y):
        if x < R and y < R:
            return (x - R) ** 2 + (y - R) ** 2 <= R * R
        if x >= size - R and y < R:
            return (x - (size - R)) ** 2 + (y - R) ** 2 <= R * R
        if x < R and y >= size - R:
            return (x - R) ** 2 + (y - (size - R)) ** 2 <= R * R
        if x >= size - R and y >= size - R:
            return (x - (size - R)) ** 2 + (y - (size - R)) ** 2 <= R * R
        return True

    # 背景: 垂直渐变 + 圆角裁剪
    for y in range(size):
        col = hexcolor(bg_at(y))
        for x in range(size):
            if inside_rounded(x, y):
                img.put(col, to=(x, y, x + 1, y + 1))

    # 三层错位拼接块 (半透明白, 与背景混合)
    layers = [
        (0.30, size * 0.22, size * 0.38, size * 0.68, size * 0.54),
        (0.55, size * 0.28, size * 0.31, size * 0.74, size * 0.48),
        (0.95, size * 0.34, size * 0.24, size * 0.80, size * 0.42),
    ]
    for alpha, x0, y0, x1, y1 in layers:
        for yy in range(int(y0), int(y1)):
            col = hexcolor(blend(WHITE, bg_at(yy), alpha))
            img.put(col, to=(int(x0), yy, int(x1), yy + 1))

    # 顶层中间: "媒体文件"占位 (淡蓝), 表达外层是视频/图片/文档
    mx0, my0, mx1, my1 = size * 0.46, size * 0.28, size * 0.72, size * 0.40
    for yy in range(int(my0), int(my1)):
        img.put(hexcolor(LAYER_MID), to=(int(mx0), yy, int(mx1), yy + 1))

    # 右下角迷你锁 (金黄), 表达加密
    lx0, ly0, lx1, ly1 = size * 0.70, size * 0.62, size * 0.86, size * 0.80
    for yy in range(int(ly0), int(ly1)):
        img.put(hexcolor(LOCK), to=(int(lx0), yy, int(lx1), yy + 1))

    # 锁梁: U 形 (顶部半圆 + 两侧下行竖线)
    arc_cx = (lx0 + lx1) / 2
    arc_r = (lx1 - lx0) / 2 * 0.55
    arc_top_y = ly0 - arc_r * 2
    arc_center_y = arc_top_y + arc_r
    half_sw = max(2, int(size * 0.025)) / 2
    for yy in range(int(arc_top_y), int(ly0) + 1):
        for xx in range(int(lx0), int(lx1) + 1):
            dx = xx - arc_cx
            if yy < arc_center_y:
                d = (dx ** 2 + (yy - arc_center_y) ** 2) ** 0.5
                if abs(d - arc_r) <= half_sw:
                    img.put(hexcolor(LOCK), to=(xx, yy, xx + 1, yy + 1))
            else:
                if abs(abs(dx) - arc_r) <= half_sw:
                    img.put(hexcolor(LOCK), to=(xx, yy, xx + 1, yy + 1))

    # 锁孔
    hole = size * 0.025
    cx = arc_cx
    cy = (ly0 + ly1) / 2
    for yy in range(int(cy - hole - 2), int(cy + hole + 2)):
        for xx in range(int(cx - hole - 2), int(cx + hole + 2)):
            if (xx - cx) ** 2 + (yy - cy) ** 2 <= hole * hole:
                img.put(hexcolor(LOCK_DARK), to=(xx, yy, xx + 1, yy + 1))


def _set_window_icon(root):
    """为主窗口设置图标 (运行时用 PhotoImage.put 动态绘制像素, 零文件依赖)。

    图标为深蓝渐变底 + 三层白色错位拼接块 + 右下角黄色锁,
    寓意多格式拼接与内层加密。设置失败静默降级为默认图标。
    """
    try:
        size = 128
        icon = tk.PhotoImage(width=size, height=size)
        _draw_icon_pixels(icon, size)
        root.iconphoto(True, icon)
        root._app_icon = icon  # 防 GC
    except Exception:
        pass


if __name__ == '__main__':
    launch_gui()
