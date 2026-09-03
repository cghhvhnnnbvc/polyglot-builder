# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Polyglot Builder - 图形界面 v1.1

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
from typing import Dict, List, Optional

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

from polyglot_ledger import (FIELD_LABELS, NETDISK_CHOICES, LedgerError,
                             LedgerRecord, append_record, create_ledger,
                             html_view_path, load_records, now_str,
                             normalize_ledger_path, open_ledger,
                             resolve_ledger_path, save_configured_path,
                             save_records)


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
    # 正向平台守卫: 令 mypy(Linux/CI ubuntu) 将下方 Windows 专用 ctypes 代码
    # 判定为不可达而跳过 (这些名字仅在模块级 win32 分支定义)。
    if sys.platform == 'win32':
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

# 次级功能按钮 (资源台账): 浅蓝底 + 蓝字, 与主行动蓝同色系但明显区分,
# 在浅灰背景上对比清晰 (原本的中灰底白字对比不足, 不易识别)
C_LEDGER       = '#E8F1FD'
C_LEDGER_H     = '#D6E6FB'
C_LEDGER_A     = '#C3DAF8'

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
            # 用自身的悬停色而非硬编码主色, 否则自定义颜色的按钮 (如取消/台账)
            # 点击后会闪一下蓝色
            self._draw(self._hover_bg)
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
# Tooltip (鼠标悬浮提示, 零依赖)
# ============================================================
class Tooltip:
    """为任意 tkinter 控件附加鼠标悬浮提示。

    用法:
        Tooltip(widget, text='说明文字')

    鼠标移入 (Enter) 延迟约 0.5s 后在光标附近显示提示卡,
    移出 (Leave) 时自动关闭。支持换行文本 (用 \\n)。

    风格与主 UI 一致: 白底、浅灰描边、Segoe UI 9pt 文本、无
    立体浮雕, 仅靠 1px 边框营造边界 (主 UI 也是这种扁平处理)。
    """

    # 配色常量, 与顶部 C_BG / C_BORDER / C_TEXT 保持视觉一致
    _BG = '#FFFFFF'        # 白底, 与卡片同色
    _FG = '#333333'        # 与主文本色接近
    _BORDER = '#D9D9DE'    # 浅灰描边, 与卡片边框同色
    _FONT = ('Segoe UI', 9)  # 跨平台, _resolve_fonts 已优先选用本地字体

    def __init__(self, widget, text: str, delay_ms: int = 500):
        self._widget = widget
        self._text = text
        self._delay = delay_ms
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _event):
        self._hide(None)
        self._after_id = self._widget.after(self._delay, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 8
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)   # 无边框窗口
        self._tip.wm_geometry(f'+{x}+{y}')

        # 外层 Frame 负责 1px 浅灰描边 (主 UI 同款边框色)
        frame = tk.Frame(
            self._tip, background=self._BORDER, bd=0, highlightthickness=0
        )
        frame.pack()

        # 内层 Label 承载内容, 白底无独立边框, 整体看上去仍是 1px 描边
        label = tk.Label(
            frame, text=self._text, justify=tk.LEFT,
            background=self._BG, foreground=self._FG,
            font=self._FONT,
            padx=10, pady=8, wraplength=320,
            bd=0, highlightthickness=0,
        )
        label.pack(padx=1, pady=1)  # 1px 间隙 = 描边

    def _hide(self, _event):
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


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
# 资源台账 - 记账对话框
# ============================================================
class LedgerRecordDialog(tk.Toplevel):
    """台账记录编辑对话框 (新增与编辑共用)。

    新增时: 文件名/大小/时间由调用方预填, 其余字段用户补充。
    编辑时: 传入 record, 全部字段回填可改。
    RAR 密码仅作记录: 加密由 WinRAR 负责, 本工具不参与加解密。
    点"保存记录"后 self.record 为 LedgerRecord, 跳过/取消则为 None。
    """

    # (字段名, 标签, 右侧提示文字); netdisk 用下拉, 其余为输入框。
    # 注: 不用 PlaceholderEntry —— 它会把占位文字写进绑定的 textvariable,
    # 导致未填写的字段被存成提示文字; 这里改用常驻的右侧灰色提示。
    _FIELDS = [
        ('name', '资源名称', '这是什么资源, 如: 某游戏整合包'),
        ('filename', '文件名', '构建后自动填入, 也可手改'),
        ('size', '大小', '如: 1.5 GB'),
        ('date', '记录时间', '留空则自动填当前时间'),
        ('netdisk', '网盘', '下拉选择或直接输入'),
        ('netdisk_path', '网盘位置', '如: /我的资源/2026/游戏'),
        ('share_link', '分享链接', '可选'),
        ('share_code', '提取码', '可选'),
        ('rar_password', 'RAR 密码', 'WinRAR 解压密码 (仅本地明文记录)'),
        ('note', '备注', '可选'),
    ]

    def __init__(self, parent, *, filename: str = '', size: str = '',
                 date: str = '', record: Optional[LedgerRecord] = None):
        super().__init__(parent)
        editing = record is not None
        if editing:
            title = '编辑台账记录'
        elif filename:
            title = '记入资源台账'
        else:
            title = '新增台账记录'
        self.title(title)
        self.configure(bg=C_CARD, padx=18, pady=14)
        self.resizable(False, False)
        self.transient(parent)
        self.record: Optional[LedgerRecord] = None
        self._vars: Dict[str, tk.StringVar] = {}

        # 初始值: 编辑时全部回填; 新增时用构建产物预填 (手动新增则为空)
        if record is not None:
            initial = {k: str(getattr(record, k, '')) for k, _l, _h in self._FIELDS}
        else:
            initial = {'filename': filename, 'size': size, 'date': date}

        for i, (key, label, hint) in enumerate(self._FIELDS):
            ttk.Label(self, text=label, font=FONT_LABEL,
                      foreground=C_TEXT).grid(row=i, column=0, sticky='w',
                                              padx=(0, 10), pady=4)
            var = tk.StringVar(value=initial.get(key, ''))
            self._vars[key] = var
            if key == 'netdisk':
                widget: tk.Widget = ttk.Combobox(
                    self, textvariable=var, values=NETDISK_CHOICES,
                    font=FONT_ENTRY, width=32)
            else:
                widget = ttk.Entry(self, textvariable=var, font=FONT_ENTRY,
                                   width=34)
            widget.grid(row=i, column=1, sticky='w', pady=4)
            ttk.Label(self, text=hint, font=FONT_HINT,
                      foreground=C_TEXT_SEC).grid(row=i, column=2, sticky='w',
                                                 padx=(10, 0), pady=4)

        last = len(self._FIELDS)
        ttk.Label(self, text='提示: 密码明文保存在本地台账文件中, 请勿上传网盘或发送给他人',
                  font=FONT_HINT, foreground=C_TEXT_SEC).grid(
                      row=last, column=0, columnspan=3, sticky='w', pady=(10, 8))

        btns = ttk.Frame(self)
        btns.grid(row=last + 1, column=0, columnspan=3, sticky='e')
        ttk.Button(btns, text='跳过', width=8,
                   command=self._on_cancel).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btns, text='保存记录', width=10,
                   command=self._on_save).pack(side=tk.LEFT)

        self.bind('<Return>', lambda e: self._on_save())
        self.bind('<Escape>', lambda e: self._on_cancel())

    def _on_save(self) -> None:
        vals = {k: v.get().strip() for k, v in self._vars.items()}
        if not vals.get('date'):
            vals['date'] = now_str()   # 记录时间留空则自动填当前时间
        self.record = LedgerRecord(**vals)
        self.destroy()

    def _on_cancel(self) -> None:
        self.record = None
        self.destroy()


# ============================================================
# 资源台账 - 管理窗口 (查看 / 新增 / 编辑 / 删除)
# ============================================================
class LedgerManagerDialog(tk.Toplevel):
    """资源台账管理窗口。

    为何不在网页里改: 浏览器打开的 file:// 页面没有写本地文件的权限
    (File System Access API 在 file:// 下不可用, Firefox/Safari 也不支持),
    因此台账的增删改统一在本窗口完成并即时写回 HTML;
    网页版仅用于查看/搜索/密码遮罩/一键复制/导出 CSV。
    """

    # (字段, 列标题, 列宽)
    COLUMNS = [('name', '资源名称', 190),
               ('filename', '文件名', 150),
               ('netdisk', '网盘', 90),
               ('netdisk_path', '网盘位置', 180),
               ('date', '记录时间', 110)]

    def __init__(self, parent, path: str):
        super().__init__(parent)
        self.path = path
        self.records: List[LedgerRecord] = []
        self._iid_to_index: Dict[str, int] = {}

        self.title('资源台账管理')
        self.configure(bg=C_BG)
        self.geometry('900x540')
        self.minsize(760, 440)
        self.transient(parent)

        self._load()
        self._build_ui()
        self._refresh()

    # --------------------------------------------------------
    # 数据读写
    # --------------------------------------------------------
    def _load(self) -> None:
        try:
            self.records = load_records(self.path)
        except LedgerError as e:
            self.records = []
            messagebox.showerror('读取台账失败', str(e), parent=self)

    def _save(self) -> bool:
        """整体写回台账文件; 失败返回 False (不中断窗口使用)。"""
        try:
            save_records(self.path, self.records)
        except LedgerError as e:
            messagebox.showerror('保存台账失败', str(e), parent=self)
            return False
        self._refresh()
        return True

    # --------------------------------------------------------
    # 界面
    # --------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure('Ledger.Treeview', rowheight=26, font=FONT_LABEL)
        style.configure('Ledger.Treeview.Heading', font=FONT_SECTION)

        top = ttk.Frame(self, padding=(14, 12, 14, 8))
        top.pack(fill=tk.X)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text='搜索', font=FONT_LABEL,
                  foreground=C_TEXT_SEC).grid(row=0, column=0, sticky='w',
                                              padx=(0, 8))
        self._query = tk.StringVar()
        self._query.trace_add('write', lambda *a: self._refresh())
        # 不用 PlaceholderEntry: 它会把占位文字写进 textvariable, 干扰搜索
        entry = ttk.Entry(top, textvariable=self._query, font=FONT_ENTRY, width=34)
        entry.grid(row=0, column=1, sticky='w')
        ttk.Label(top, text='按任意字段过滤 (名称/文件名/网盘/位置/密码/备注)',
                  font=FONT_HINT, foreground=C_TEXT_SEC).grid(
                      row=0, column=2, sticky='w', padx=(10, 0))

        mid = ttk.Frame(self, padding=(14, 0, 14, 8))
        mid.pack(fill=tk.BOTH, expand=True)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(mid, columns=cols, show='headings',
                                 style='Ledger.Treeview', selectmode='browse')
        for key, label, width in self.COLUMNS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor='w')
        vsb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind('<Double-1>', lambda e: self._on_edit())

        bottom = ttk.Frame(self, padding=(14, 0, 14, 10))
        bottom.pack(fill=tk.X)
        ttk.Button(bottom, text='新增记录',
                   command=self._on_add).pack(side=tk.LEFT)
        ttk.Button(bottom, text='编辑',
                   command=self._on_edit).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text='删除',
                   command=self._on_delete).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text='在浏览器中打开',
                   command=self._on_open_browser).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(bottom, text='关闭',
                   command=self.destroy).pack(side=tk.RIGHT)
        self._status_lbl = ttk.Label(bottom, text='', font=FONT_HINT,
                                     foreground=C_TEXT_SEC)
        self._status_lbl.pack(side=tk.RIGHT, padx=(0, 12))

        ttk.Label(self, text=f'台账数据: {self.path}', font=FONT_HINT,
                  foreground=C_TEXT_SEC).pack(anchor='w', padx=14, pady=(0, 2))
        ttk.Label(self, text=f'查看页 (自动生成, 可删): {html_view_path(self.path)}',
                  font=FONT_HINT, foreground=C_TEXT_SEC).pack(
                      anchor='w', padx=14, pady=(0, 12))
        self.bind('<Escape>', lambda e: self.destroy())

    # --------------------------------------------------------
    # 交互
    # --------------------------------------------------------
    def _refresh(self) -> None:
        """按搜索词重建表格, 并维护 iid -> 记录索引 的映射。"""
        q = self._query.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._iid_to_index = {}
        for idx, rec in enumerate(self.records):
            if q:
                haystack = ' '.join(str(getattr(rec, k, ''))
                                    for k, _lbl in FIELD_LABELS).lower()
                if q not in haystack:
                    continue
            iid = self.tree.insert(
                '', 'end',
                values=[getattr(rec, k, '') or '—' for k, _l, _w in self.COLUMNS])
            self._iid_to_index[iid] = idx
        self._set_status()

    def _set_status(self) -> None:
        if not hasattr(self, '_status_lbl'):
            return
        shown = len(self.tree.get_children())
        total = len(self.records)
        suffix = '' if shown == total else f' (筛出 {shown} 条)'
        self._status_lbl.configure(text=f'共 {total} 条{suffix}')

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._iid_to_index.get(sel[0])

    def _on_add(self) -> None:
        dlg = LedgerRecordDialog(self, date=now_str())
        dlg.grab_set()
        self.wait_window(dlg)
        if dlg.record is None:
            return
        self.records.append(dlg.record)
        self._save()

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo('提示', '请先在列表中选择一条记录', parent=self)
            return
        dlg = LedgerRecordDialog(self, record=self.records[idx])
        dlg.grab_set()
        self.wait_window(dlg)
        if dlg.record is None:
            return
        self.records[idx] = dlg.record
        self._save()

    def _on_delete(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo('提示', '请先在列表中选择一条记录', parent=self)
            return
        rec = self.records[idx]
        label = rec.name or rec.filename or '(未命名记录)'
        if not messagebox.askyesno(
                '确认删除',
                f'确定删除这条台账记录吗？\n\n{label}\n\n'
                '(仅删除台账记录, 不会影响已构建的文件)', parent=self):
            return
        del self.records[idx]
        self._save()

    def _on_open_browser(self) -> None:
        try:
            open_ledger(self.path)
        except Exception as e:  # webbrowser 在部分环境可能报错
            messagebox.showerror('打开失败', f'无法打开台账文件:\n{e}', parent=self)


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

        仅控制下拉框可用性 (勾选即变为可点/readonly), 保证下拉立即可用。
        ffmpeg 缺失的检测与引导下载延迟到构建时 (_run), 不阻塞本操作。
        """
        enabled = self._compress_var.get()
        self._quality_combo.configure(
            state='readonly' if enabled else 'disabled')

    def _prompt_download_ffmpeg(self):
        """未检测到 ffmpeg 时, 弹出提示框让用户选择下载/取消。

        本次构建已因此中断: 进入时先恢复 UI 可用状态 (build 重新可点,
        cancel 禁用), 避免移除旧的 _on_error 弹窗后按钮卡在禁用态。
        """
        # 中断本次构建: 先解锁 UI
        self.build_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        self._set_status('需要 ffmpeg')
        # 镜像选项文本
        mirror_names = [name for name, _url in FFMPEG_MIRRORS]
        result = messagebox.askquestion(
            '需要 ffmpeg',
            '未检测到 ffmpeg。\n\n'
            '压缩表面视频需要 ffmpeg 组件。\n'
            '是否从网络下载并自动安装？\n\n'
            f'默认镜像: {mirror_names[0]}\n'
            '(如下载缓慢可切换国内镜像)\n\n'
            '点击"否"将取消压缩。\n'
            '下载安装完成后, 请重新点击「开始构建」。',
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
        # 下拉框: 与输入框视觉一致, 白底无高亮底纹
        s.configure('TCombobox', font=FONT_ENTRY, padding=(8, 6),
                     borderwidth=1, relief='flat', fieldbackground=C_CARD,
                     background=C_CARD, foreground=C_TEXT, arrowcolor=C_TEXT_SEC)
        s.map('TCombobox',
              fieldbackground=[('readonly', C_CARD), ('disabled', C_BG),
                               ('focus', C_CARD), ('active', C_CARD)],
              background=[('readonly', C_CARD), ('disabled', C_BG),
                          ('active', C_CARD)],
              foreground=[('readonly', C_TEXT), ('disabled', C_TEXT_SEC)],
              selectbackground=[('readonly', C_CARD), ('focus', C_CARD)],
              selectforeground=[('readonly', C_TEXT), ('focus', C_TEXT)],
              arrowcolor=[('disabled', C_TEXT_SEC)])
        # 下拉列表项 (TCombobox 的 Listbox): 选中态主色蓝底白字, 取消系统蓝高亮
        s.configure('ComboboxPopdownFrame', background=C_CARD)
        s.configure('TCombobox.Listbox', background=C_CARD, foreground=C_TEXT,
                     selectbackground=C_PRIMARY, selectforeground='white',
                     font=FONT_ENTRY, borderwidth=0)
        # 浏览按钮
        s.configure('TButton', font=FONT_BROWSE, padding=(12, 4),
                     borderwidth=0, relief='flat')
        # 进度条
        s.configure('Horizontal.TProgressbar', thickness=12,
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

        # === 标题行 (左: 标题, 右: 资源台账入口) ===
        # 台账是独立功能入口, 不属于构建动作, 故不放在构建按钮行;
        # 放标题行右侧可避开该行与文件卡片内按钮的边距不一致问题。
        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 16))
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text='Polyglot Builder',
                          font=FONT_TITLE, foreground=C_TEXT)
        title.grid(row=0, column=0, sticky='w')

        self.ledger_btn = RoundedButton(
            header, text='资源台账', command=self._open_ledger,
            bg=C_LEDGER, fg=C_PRIMARY, hover_bg=C_LEDGER_H,
            active_bg=C_LEDGER_A, canvas_bg=C_BG,
            font=FONT_ENTRY, width=124, height=34, radius=6
        )
        self.ledger_btn.grid(row=0, column=1, sticky='e')

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
        # 并排显示: 所有控件在左侧单行排列, 无右侧文字, 避免浅灰底纹突出
        # 带 ⓘ 图标提示该控件有悬浮说明
        opt_frame = ttk.Frame(main)
        opt_frame.grid(row=2, column=0, sticky='ew', pady=(0, 10))

        # 自定义复选框: 用 Unicode 方框字符代替系统 indicator,
        # 方框随字号放大, 无 image 依赖 (跨多 Tk 根安全)。
        CB_BOX = '\u2610'   # ☐ 未选中
        CB_CHECK = '\u2611' # ☑ 选中

        def _mk_checkbox(master, label, var, cmd=None, info_tip=None):
            """创建带角标 ⓘ 的复选框。

            ⓘ 不放在 text 里 (与正文同字号会偏大), 而是用独立小 Label
            紧贴 Checkbutton 右侧, 字号 8pt + 淡灰色, 视觉上像数学角标。
            """
            full = tk.StringVar()

            def _refresh(*_a):
                sym = CB_CHECK if var.get() else CB_BOX
                full.set(f'{sym}  {label}')

            var.trace_add('write', _refresh)
            _refresh()
            cb = tk.Checkbutton(
                master, textvariable=full, variable=var,
                bg=C_BG, fg=C_TEXT, activebackground=C_BG,
                activeforeground=C_TEXT, selectcolor=C_BG,
                font=(FONT_ENTRY[0], 11),  # 与卡片输入框一致, 方框随字放大
                bd=0, highlightthickness=0, relief='flat',
                indicatoron=False, compound=tk.LEFT,
                command=cmd,
            )
            # 角标 ⓘ: 小字号 + 淡灰色 + 顶部对齐 (贴近文字顶)
            info = tk.Label(
                master, text='\u24d8',
                bg=C_BG, fg=C_TEXT_SEC,
                font=(FONT_ENTRY[0], 8, 'bold'),
                cursor='question_arrow',
                bd=0, highlightthickness=0,
            )
            if info_tip:
                Tooltip(info, info_tip)
            return cb, info

        self._deflate_var = tk.BooleanVar(value=False)
        deflate_cb, deflate_info = _mk_checkbox(
            opt_frame, 'Deflate 压缩', self._deflate_var,
            info_tip='对内部 RAR 数据使用 Deflate 压缩。\n'
                     '默认关闭 (RAR 本身已高度压缩, 再压收益极小且更耗时)。\n'
                     '通常无需开启。')
        deflate_cb.pack(side=tk.LEFT, padx=(0, 2))
        deflate_info.pack(side=tk.LEFT, padx=(0, 22), pady=(4, 0))
        Tooltip(deflate_cb,
                '对内部 RAR 数据使用 Deflate 压缩。\n'
                '默认关闭 (RAR 本身已高度压缩, 再压收益极小且更耗时)。\n'
                '通常无需开启。')

        self._compress_var = tk.BooleanVar(value=False)
        compress_cb, compress_info = _mk_checkbox(
            opt_frame, '压缩表面视频', self._compress_var,
            cmd=self._on_compress_toggle,
            info_tip='用 ffmpeg 压缩外层视频, 减小最终文件体积, 提高隐蔽性。\n\n'
                     '用长视频做外层并压缩, 可避免"表面是小文件却占用几个 G"\n'
                     '的违和感, 降低被平台判定为异常文件的风险。')
        compress_cb.pack(side=tk.LEFT, padx=(0, 2))
        compress_info.pack(side=tk.LEFT, padx=(0, 12), pady=(4, 0))
        Tooltip(compress_cb,
                '用 ffmpeg 压缩外层视频, 减小最终文件体积, 提高隐蔽性。\n\n'
                '用长视频做外层并压缩, 可避免"表面是小文件却占用几个 G"\n'
                '的违和感, 降低被平台判定为异常文件的风险。')

        # 质量档位下拉 (仅勾选时启用; 一直保持可点, 未勾选时灰显)
        self._quality_var = tk.StringVar(value=DEFAULT_VIDEO_QUALITY)
        quality_labels = [f'{k} - {VIDEO_QUALITY[k][2]}' for k in VIDEO_QUALITY]
        self._quality_combo = ttk.Combobox(
            opt_frame, state='disabled', width=26,  # 容纳 'medium - 中 (1.5Mbps, 720p)' 全长
            textvariable=self._quality_var, values=quality_labels,
            font=FONT_ENTRY,
        )
        self._quality_combo.pack(side=tk.LEFT, padx=(0, 12))
        self._quality_combo.set(quality_labels[0])
        self._quality_labels = quality_labels
        Tooltip(self._quality_combo,
                '压缩质量档位: 码率越低 / 分辨率越低, 体积越小。\n\n'
                '· 高: 3Mbps, 1080p (画面清晰, 压缩少)\n'
                '· 中: 1.5Mbps, 720p (体积与清晰度平衡, 推荐)\n'
                '· 低: 0.8Mbps, 480p (体积最小, 画面略糊)\n\n'
                '仅勾选"压缩表面视频"后可用。')

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

        # === 进度条 + 状态文字 ===
        prog_frame = ttk.Frame(main)
        prog_frame.grid(row=4, column=0, sticky='ew', pady=(0, 12))
        prog_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(
            prog_frame, mode='determinate', style='Horizontal.TProgressbar'
        )
        self.progress.grid(row=0, column=0, sticky='ew', pady=(0, 6))

        self.progress_lbl = ttk.Label(
            prog_frame, text='就绪', font=FONT_STATUS, foreground=C_TEXT_SEC
        )
        self.progress_lbl.grid(row=1, column=0, sticky='w')

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
        # 导出日志按钮 (将日志文本另存为 .txt, 便于事后排查/反馈)
        tk.Button(
            log_bar, text='导出', font=(FONT_LABEL[0], 8),
            bg='#E8E8ED', fg=C_TEXT_SEC, activebackground='#DCDCE2',
            activeforeground=C_TEXT_SEC, relief='flat', bd=0,
            cursor='hand2', padx=8, pady=1, command=self._export_log
        ).pack(side=tk.RIGHT, padx=(0, 8), pady=(3, 0))

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

    def _export_log(self):
        """将日志文本另存为 .txt 文件 (便于事后排查/反馈问题)。"""
        path = filedialog.asksaveasfilename(
            title='导出日志',
            defaultextension='.txt',
            filetypes=[('文本文件', '*.txt'), ('所有文件', '*.*')],
            initialfile=f'polyglot_log_{time.strftime("%Y%m%d_%H%M%S")}.txt',
        )
        if not path:
            return
        try:
            content = self.log.get('1.0', tk.END)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        except OSError as e:
            messagebox.showerror('导出失败', f'无法写入日志文件:\n{e}')
            return
        self._log(f'日志已导出: {path}', 'success')

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
    @staticmethod
    def _entry_value(var, entry) -> str:
        """输入框的实际值: 仍在显示占位提示时视为空。

        PlaceholderEntry 初始化时会把占位文字 insert 进输入框, 绑定的
        textvariable 因此也含有提示文字; 直接用 var.get() 会把提示当成用户输入
        (导致"外层文件不存在: 选择视频/图片/文档"这类莫名错误)。
        """
        if entry is not None and not entry.has_value:
            return ''
        return var.get().strip()

    def _start_build(self):
        outer = self._entry_value(self._outer_path, self._outer_entry)
        rar   = self._entry_value(self._rar_path, self._rar_entry)
        out   = self._entry_value(self._output_path, self._out_entry)

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
                # 校验 ffmpeg 可用; 缺失则引导下载 (在主线程弹窗, 中断本次构建)
                if not find_ffmpeg():
                    self._log_async('未检测到 ffmpeg, 无法压缩表面视频。', 'warning')
                    self.root.after(0, self._prompt_download_ffmpeg)
                    return
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

        # 构建成功 -> 询问是否记入资源台账 (文件名/大小/时间自动预填)
        if messagebox.askyesno(
                '记入资源台账',
                '要把这次构建记入资源台账吗？\n\n'
                '台账记录资源名称、网盘位置与 RAR 密码,\n'
                '资源多了也不会记混。\n\n'
                '(密码仅明文保存在本地台账文件中, 不会随资源上传)'):
            self._prompt_ledger_record(out, size_str)

    # --------------------------------------------------------
    # 资源台账
    # --------------------------------------------------------
    def _open_ledger(self) -> None:
        """打开资源台账管理窗口 (可增删改); 台账不存在时先引导创建。"""
        path = normalize_ledger_path(resolve_ledger_path())
        if not os.path.isfile(path):
            if not messagebox.askyesno(
                    '创建资源台账',
                    '还没有资源台账文件。\n\n'
                    '台账记录每个资源的名称、网盘位置与 RAR 密码,\n'
                    '可在管理窗口中增删改, 也可用浏览器打开查看\n'
                    '(支持搜索 / 密码遮罩 / 导出 CSV)。\n\n'
                    '接下来请选择台账数据文件 (.json) 的保存位置;\n'
                    '同名的 .html 查看页会自动生成在旁边。\n\n'
                    '是否现在创建？'):
                return
            chosen = filedialog.asksaveasfilename(
                title='选择台账数据文件保存位置', defaultextension='.json',
                initialfile=os.path.basename(path),
                initialdir=os.path.dirname(path),
                filetypes=[('资源台账数据 (*.json)', '*.json')])
            if not chosen:
                return
            path = normalize_ledger_path(chosen)
            try:
                create_ledger(path)
            except LedgerError as e:
                messagebox.showerror('创建失败', str(e))
                return
            save_configured_path(path)
            self._log_async(f'已创建资源台账: {path}', 'success')
        LedgerManagerDialog(self.root, path)

    def _prompt_ledger_record(self, out: str, size_str: str) -> None:
        """弹出记账对话框并写入台账 (台账不存在时自动在默认位置创建)。"""
        dlg = LedgerRecordDialog(self.root, filename=os.path.basename(out),
                                 size=size_str, date=now_str())
        dlg.grab_set()
        self.root.wait_window(dlg)
        if dlg.record is None:
            return
        path = normalize_ledger_path(resolve_ledger_path())
        try:
            append_record(path, dlg.record)
        except LedgerError as e:
            messagebox.showerror('记账失败', str(e))
            return
        save_configured_path(path)
        label = dlg.record.name or dlg.record.filename
        self._log_async(f'已记入资源台账: {label}', 'success')

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
    # DPI 感知 (高分辨率屏)，必须在 tk.Tk() 之前调用; 仅 Windows
    if sys.platform == 'win32':
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
