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

try:
    from polyglot_build import (build_polyglot, verify_polyglot, format_size,
                                COMP_STORED, COMP_DEFLATE, VERSION,
                                BuildCancelled)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from polyglot_build import (build_polyglot, verify_polyglot, format_size,
                                COMP_STORED, COMP_DEFLATE, VERSION,
                                BuildCancelled)


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
# 字体常量
# ============================================================
FONT_TITLE   = ('Segoe UI Semibold', 17)
FONT_SECTION = ('Segoe UI Semibold', 10)
FONT_LABEL   = ('Segoe UI', 9)
FONT_ENTRY   = ('Segoe UI', 10)
FONT_BTN     = ('Segoe UI Semibold', 11)
FONT_BROWSE  = ('Segoe UI', 9)
FONT_MONO    = ('Consolas', 10)
FONT_HINT    = ('Segoe UI', 9)
FONT_STATUS  = ('Consolas', 9)


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

    def configure(self, **kw):
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

        self.build_thread = None
        self.log_queue = queue.Queue()
        self._stop_event = threading.Event()

        self._setup_styles()
        self._create_widgets()
        self._poll_log_queue()

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

        # === 选项行 ===
        opt_frame = ttk.Frame(main)
        opt_frame.grid(row=2, column=0, sticky='w', pady=(0, 10))
        self._deflate_var = tk.BooleanVar(value=False)
        deflate_cb = ttk.Checkbutton(
            opt_frame, text='Deflate 压缩 (默认不压缩，RAR 已压缩无需再压)',
            variable=self._deflate_var
        )
        deflate_cb.pack(side=tk.LEFT)

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
        tk.Label(log_bar, text='  输出日志', font=('Segoe UI', 9, 'bold'),
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
            build_polyglot(outer, rar, out, callback=cb, method=method,
                           stop_event=self._stop_event)
            verify_polyglot(out, callback=cb)
            self.root.after(0, self._on_success, out)
        except BuildCancelled as e:
            self._log_async(f'构建已取消: {e}', 'warning')
            self.root.after(0, self._on_cancel)
        except Exception as e:
            self._log_async(f'构建失败: {e}', 'error')
            self.root.after(0, self._on_error, str(e))

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
    PolyglotGUI(root)
    root.mainloop()


if __name__ == '__main__':
    launch_gui()
