# -*- coding: utf-8 -*-
"""
Polyglot Builder - 图形界面 v2.2

UI 升级:
  - 窗口默认 820x680，最小 640x520
  - 标题 24px，正文 14-15px，按钮 16-18px
  - 文件选择区采用卡片式设计，更醒目
  - 纵向拉伸：文件区和按钮区固定，只有日志区拉伸
  - 更合理的间距和视觉层次
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import os
import sys
import time
import queue

try:
    from polyglot_build import build_polyglot, format_size
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from polyglot_build import build_polyglot, format_size


# ============================================================
# 字体常量 (统一调整字号)
# ============================================================
FONT_TITLE     = ('微软雅黑', 24, 'bold')    # 大标题
FONT_SECTION   = ('微软雅黑', 14, 'bold')    # 分区标题
FONT_LABEL     = ('微软雅黑', 15)            # 标签文字
FONT_ENTRY     = ('Segoe UI', 18)            # 输入框文字 (路径必须大号)
FONT_BUTTON    = ('微软雅黑', 15)            # 普通按钮
FONT_ACCENT    = ('微软雅黑', 18, 'bold')    # 重点按钮
FONT_MONO      = ('Consolas', 13)            # 日志文字
FONT_HINT      = ('微软雅黑', 13)            # 底部提示


# ============================================================
# 主界面
# ============================================================
class PolyglotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title('Polyglot Builder v2.2')
        self.root.geometry('860x720')
        self.root.minsize(680, 560)

        self.build_thread = None
        self.log_queue = queue.Queue()

        self._setup_styles()
        self._create_widgets()
        self._poll_log_queue()

    # --------------------------------------------------------
    # 样式
    # --------------------------------------------------------
    def _setup_styles(self):
        self.style = ttk.Style()
        if 'clam' in self.style.theme_names():
            self.style.theme_use('clam')

        s = self.style
        s.configure('TFrame',                 background='#f5f5f5')
        s.configure('TLabel',                 background='#f5f5f5', font=FONT_LABEL)
        s.configure('TEntry',                 font=FONT_ENTRY, padding=10)

        s.configure('TLabelframe.Label',      font=FONT_SECTION, foreground='#444')
        s.configure('TLabelframe',            background='#f5f5f5')

        s.configure('TButton',                font=FONT_BUTTON, padding=(14, 8))
        s.configure(
            'Accent.TButton',
            font=FONT_ACCENT,
            foreground='white',
            background='#007bff',
            padding=(24, 14)
        )
        s.map(
            'Accent.TButton',
            background=[('pressed', '#0056b3'), ('active', '#0069d9')],
            foreground=[('pressed', 'white'), ('active', 'white')]
        )
        s.configure(
            'Horizontal.TProgressbar',
            thickness=24,
            troughcolor='#e9ecef',
            background='#28a745'
        )

    # --------------------------------------------------------
    # 创建组件
    # --------------------------------------------------------
    def _create_widgets(self):
        # 主容器 (左右 padding 较大，内容区域纵向可扩展)
        main = ttk.Frame(self.root, padding=(28, 20, 28, 16))
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        # 行 0: 标题 (固定)
        # 行 1: 文件选择区 (固定)
        # 行 2: 操作按钮 (固定)
        # 行 3: 进度条 (固定)
        # 行 4: 日志区 (可拉伸)
        # 行 5: 提示 (固定)
        main.rowconfigure(4, weight=1)

        # === 标题 ===
        title = ttk.Label(main, text='🛡️  Polyglot Builder', font=FONT_TITLE, foreground='#1a1a1a')
        title.grid(row=0, column=0, sticky='w', pady=(0, 18))

        # === 文件选择区 (卡片) ===
        card = ttk.LabelFrame(main, text='  文件选择  ', padding=(18, 14))
        card.grid(row=1, column=0, sticky='ew', pady=(0, 14))
        card.columnconfigure(1, weight=1)

        self._outer_path = tk.StringVar()
        self._rar_path   = tk.StringVar()
        self._output_path = tk.StringVar()

        self._file_row(card, 0, '外层文件 (伪装) ', self._outer_path,
                        [('视频文件', '*.mp4 *.mkv *.avi'),
                         ('图片文件', '*.jpg *.jpeg *.png *.bmp'),
                         ('文档文件', '*.pdf'),
                         ('音频文件', '*.mp3 *.flac *.wav'),
                         ('所有文件', '*.*')])

        self._file_row(card, 1, '加密 RAR (内层)', self._rar_path,
                        [('RAR 压缩包', '*.rar'), ('所有文件', '*.*')])

        self._file_row(card, 2, '输出文件名 (可选)', self._output_path,
                        [('MP4 文件', '*.mp4'), ('所有文件', '*.*')],
                        optional=True, save_dialog=True)

        self._outer_path.trace_add('write', lambda *a: self._auto_output())

        # === 操作按钮 ===
        self.build_btn = ttk.Button(main, text='🔨  开 始 构 建', style='Accent.TButton', command=self._start_build)
        self.build_btn.grid(row=2, column=0, sticky='ew', pady=(0, 14), ipady=6)

        # === 进度条 ===
        prog = ttk.Frame(main)
        prog.grid(row=3, column=0, sticky='ew', pady=(0, 14))
        prog.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(prog, mode='determinate', style='Horizontal.TProgressbar')
        self.progress.grid(row=0, column=0, sticky='ew')

        self.progress_lbl = ttk.Label(prog, text='就绪', font=('Consolas', 12), foreground='#666')
        self.progress_lbl.grid(row=0, column=1, padx=(12, 0))

        # === 日志区 (唯一可拉伸) ===
        log_box = ttk.LabelFrame(main, text=' 处理日志 ', padding=(8, 8))
        log_box.grid(row=4, column=0, sticky='nsew', pady=(0, 12))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log = tk.Text(
            log_box,
            font=FONT_MONO,
            bg='#1e1e1e', fg='#d4d4d4',
            insertbackground='#d4d4d4',
            relief='flat',
            padx=10, pady=8,
            height=7,
            wrap=tk.WORD,
            state=tk.DISABLED,
            undo=False
        )
        log_scroll = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)

        self.log.grid(     row=0, column=0, sticky='nsew')
        log_scroll.grid(  row=0, column=1, sticky='ns')

        self.log.tag_configure('info',    foreground='#d4d4d4')
        self.log.tag_configure('success', foreground='#4ec9b0')
        self.log.tag_configure('warning', foreground='#dcdcaa')
        self.log.tag_configure('error',   foreground='#f44747')

        # === 底部提示 ===
        hint = ttk.Label(main,
                         text='💡 改后缀 .zip  →  WinRAR 打开  →  输入 RAR 密码  →  得到游戏',
                         font=FONT_HINT, foreground='#888', anchor=tk.CENTER)
        hint.grid(row=5, column=0, sticky='ew')

    # --------------------------------------------------------
    # 文件选择行内部组件
    # --------------------------------------------------------
    def _file_row(self, parent, row, text, var, filetypes, optional=False, save_dialog=False):
        lbl = ttk.Label(parent, text=text, width=19, anchor='e')
        lbl.grid(row=row, column=0, sticky='e', padx=(0, 10), pady=8)

        entry = ttk.Entry(parent, textvariable=var, font=FONT_ENTRY)
        entry.grid(row=row, column=1, sticky='ew', pady=8)

        btn_text = '另存为...' if save_dialog else '浏览...'
        btn = ttk.Button(parent, text=btn_text, width=9)
        btn.grid(row=row, column=2, padx=(10, 0), pady=8)
        btn.configure(command=lambda: self._browse(var, filetypes, entry, save_dialog))

    def _auto_output(self):
        outer = self._outer_path.get()
        if outer and not self._output_path.get():
            self._output_path.set(outer)

    def _browse(self, var, filetypes, entry=None, save_dialog=False):
        if save_dialog:
            # 另存为对话框: 可选保存位置 + 自定义文件名
            path = filedialog.asksaveasfilename(
                title='选择保存位置和文件名',
                filetypes=filetypes,
                defaultextension='.mp4'
            )
        else:
            path = filedialog.askopenfilename(title='选择文件', filetypes=filetypes)
        if path:
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

        # 锁 UI
        self.build_btn.configure(state=tk.DISABLED)
        self.progress['value'] = 0
        self._set_status('准备中...')
        self.log.configure(state=tk.NORMAL)
        self.log.delete(1.0, tk.END)
        self.log.configure(state=tk.DISABLED)

        self.build_thread = threading.Thread(target=self._run, args=(outer, rar, out), daemon=True)
        self.build_thread.start()

    def _run(self, outer, rar, out):
        def cb(phase, cur, total, msg):
            if phase in ('start', 'info'):
                self._log_async(msg, 'info')
            elif phase in ('compress', 'copy') and total > 0:
                pct = cur * 100 // total
                self.root.after(0, self._set_progress, pct)
                self.root.after(0, self._set_status, msg)
            elif phase == 'done':
                self.root.after(0, self._set_progress, 100)
                self.root.after(0, self._set_status, '完成')
                self._log_async(msg, 'success')

        try:
            build_polyglot(outer, rar, out, callback=cb)
            self.root.after(0, self._on_success, out)
        except Exception as e:
            self._log_async(f'构建失败: {e}', 'error')
            self.root.after(0, self._on_error, str(e))

    def _on_success(self, out):
        self.build_btn.configure(state=tk.NORMAL)
        try:
            size_str = format_size(os.path.getsize(out))
        except OSError:
            size_str = '未知'
        messagebox.showinfo(
            '构建完成',
            f'✅ 构建成功!\n\n'
            f'文件: {os.path.basename(out)}\n'
            f'大小: {size_str}\n'
            f'路径: {out}\n\n'
            f'使用方式:\n'
            f'  1. 直接打开 → 播放/查看外层内容\n'
            f'  2. 改后缀 .zip → WinRAR 解压\n'
            f'  3. 输入 RAR 密码 → 得到最终内容'
        )

    def _on_error(self, msg):
        self.build_btn.configure(state=tk.NORMAL)
        self._set_status('失败')
        messagebox.showerror('构建失败', f'构建出错:\n\n{msg}')


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
