#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Polyglot Builder - 多格式文件拼接工具

将媒体/文档文件与加密 RAR 压缩包拼接为一个 polyglot 文件。
该文件既可以被对应的播放器/查看器正常打开（伪装成普通文件），
也可以改后缀名为 .zip 后用 WinRAR/7-Zip 解压出内层的 RAR 文件。

技术原理：
  - 外层文件（MP4/PDF/JPEG/MP3/BMP 等）在文件头部，播放器从头读取
  - ZIP 结构追加在外层文件之后，ZIP 读取器从文件尾部开始解析
  - 内层 RAR 使用 AES-256 加密，即使平台解压 ZIP 也看不到内容
  - 使用 ZIP 数据描述符（Data Descriptor）支持流式大文件处理

使用方法：
  python polyglot_build.py <外层文件> <加密RAR文件> [-o 输出文件]

示例：
  python polyglot_build.py video.mp4 game.rar
  python polyglot_build.py photo.jpg secret.rar -o output.jpg

作者: feng
日期: 2026-07-26
"""

import argparse
import zlib
import struct
import sys
import os
import time
import shutil
import threading
import zipfile
import logging
import subprocess
import urllib.request
import urllib.error
from contextlib import contextmanager
from typing import Callable, IO, List, Optional, Tuple

# 进度回调签名: (phase, current, total, message) -> None
ProgressCallback = Callable[[str, int, int, str], None]

# ============================================================
# 版本号 (单一来源: GUI / CLI / bat 均需与此保持一致)
# ============================================================
VERSION = '3.0'


# ============================================================
# 统一日志层
# ============================================================
# CLI 与 GUI 共用同一套日志通道: 模块级 logger + 统一配置入口。
# CLI 默认输出到 stdout (进度条走 progress_callback 的特化渲染),
# GUI 可复用该 logger 或直接走自己的 Text 控件。模块内统一用
# logging.getLogger(__name__) 记录阶段/错误信息, 避免裸 print 散落。
LOGGER_NAME = 'polyglot_builder'


def get_logger() -> logging.Logger:
    """返回模块统一的 logger。"""
    return logging.getLogger(LOGGER_NAME)


def setup_logging(level: int = logging.INFO,
                  log_file: Optional[str] = None) -> logging.Logger:
    """配置全局日志 (幂等)。CLI 与 GUI 共用此入口, 保证输出格式一致。

    默认挂一个简洁的 StreamHandler (stdout); 传入 log_file 时额外挂一个
    FileHandler (追加模式, UTF-8, 带时间戳/级别) 将日志持久化, 便于事后排查。
    重复调用同一 log_file 不会重复挂载。
    """
    logger = get_logger()
    # 控制台 handler: FileHandler 也是 StreamHandler 子类, 需排除后再判断
    has_console = any(isinstance(h, logging.StreamHandler)
                      and not isinstance(h, logging.FileHandler)
                      for h in logger.handlers)
    if not has_console:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter('%(message)s'))  # 简洁: 仅消息本身
        logger.addHandler(handler)
    if log_file:
        abs_path = os.path.abspath(log_file)
        has_file = any(isinstance(h, logging.FileHandler)
                       and os.path.abspath(h.baseFilename) == abs_path
                       for h in logger.handlers)
        if not has_file:
            file_handler = logging.FileHandler(
                log_file, mode='a', encoding='utf-8')
            file_handler.setFormatter(
                logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            logger.addHandler(file_handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class BuildCancelled(Exception):
    """构建被用户取消时抛出的异常。"""


def _check_stop(stop_event: Optional[threading.Event]) -> None:
    """如果 stop_event 被设置, 抛出 BuildCancelled。"""
    if stop_event is not None and stop_event.is_set():
        raise BuildCancelled('构建已被用户取消')


# ============================================================
# ZIP 常量定义
# ============================================================

LOCAL_HEADER_SIG = 0x04034b50
CENTRAL_DIR_SIG = 0x02014b50
EOCD_SIG = 0x06054b50
ZIP64_EOCD_SIG = 0x06064b50
ZIP64_EOCD_LOCATOR_SIG = 0x07064b50

# ZIP 通用标志位
FLAG_DATA_DESCRIPTOR = 0x0008  # bit 3: 使用数据描述符
FLAG_UTF8 = 0x0800             # bit 11: UTF-8 文件名

# ZIP 压缩方法
COMP_DEFLATE = 8
COMP_STORED = 0

# 分块读取大小 (8MB)
CHUNK_SIZE = 8 * 1024 * 1024

# ZIP 文件大小阈值 (ZIP64 触发条件)
ZIP64_SIZE_THRESHOLD = 0xFFFFFFFF  # 4GB - 1
ZIP64_ENTRIES_THRESHOLD = 0xFFFF    # 条目数超过 65535

# 写入 32-bit 头部字段的 ZIP64 占位标记 (规范固定为 0xFFFFFFFF, 表示"见 ZIP64 扩展")
# 与阈值分离: 阈值可被测试 patch 以触发 ZIP64 路径, 而标记始终为规范值。
ZIP64_MARKER = 0xFFFFFFFF

# RAR 文件魔数 (用于构建前的有效性提示, 非强制中断)
RAR4_MAGIC = b'Rar!\x1a\x07\x00'        # RAR 4.x
RAR5_MAGIC = b'Rar!\x1a\x07\x01\x00'    # RAR 5.x


class CRC32Calculator:
    """增量式 CRC-32 计算器，用于大文件流式处理"""
    
    def __init__(self) -> None:
        self.crc = 0
    
    def update(self, data: bytes) -> None:
        self.crc = zlib.crc32(data, self.crc)
    
    @property
    def value(self) -> int:
        return self.crc & 0xFFFFFFFF


def format_size(size_bytes: float) -> str:
    """将字节数格式化为人类可读形式"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def _dos_datetime_from_mtime(mtime: float) -> tuple[int, int]:
    """将文件 mtime (epoch 秒) 转为 ZIP 头用的 (DOS 时间, DOS 日期)。

    DOS 时间: (时<<11) | (分<<5) | (秒//2)
    DOS 日期: ((年-1980)<<9) | (月<<5) | 日
    年份早于 1980 时钳制到 1980 (DOS 纪元起点)。
    """
    t = time.localtime(mtime)
    year = max(t.tm_year, 1980)
    dos_time = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    dos_date = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    return dos_time, dos_date


def _validate_rar(rar_path: str) -> Optional[str]:
    """校验文件是否为 RAR (读魔数)。是 RAR 返回 None; 否则返回警告文本。

    仅做魔数校验 (不解析加密位), 非 RAR 时给警告, 由调用方决定是否继续。
    """
    try:
        with open(rar_path, 'rb') as f:
            head = f.read(8)
    except OSError as e:
        return f'无法读取 RAR 文件: {e}'
    if head.startswith(RAR5_MAGIC) or head.startswith(RAR4_MAGIC):
        return None
    return ('该文件不是标准 RAR (未检测到 RAR 魔数); '
            '请确认已用 WinRAR 以 RAR 格式 (非 ZIP) 压缩并设置密码。')


def generate_zip64_extra(uncompressed_size: int, compressed_size: int,
                         local_header_offset: int) -> bytes:
    """
    生成 ZIP64 扩展字段（用于中央目录和本地文件头）
    
    结构:
    - Header ID: 0x0001 (2 bytes)
    - Data Size: 根据需要的字段数量动态计算 (2 bytes)
    - 可选字段（按需出现，从低到高排列）:
      - Uncompressed Size (8 bytes)
      - Compressed Size (8 bytes)
      - Local Header Offset (8 bytes)
      - Disk Start Number (4 bytes)
    """
    extra = struct.pack('<HH', 0x0001, 0)  # placeholder
    
    fields = []
    data_size = 0
    
    # 总是需要记录未压缩大小和压缩大小（如果超过4GB）
    if uncompressed_size > ZIP64_SIZE_THRESHOLD:
        fields.append(struct.pack('<Q', uncompressed_size))
        data_size += 8
    if compressed_size > ZIP64_SIZE_THRESHOLD:
        fields.append(struct.pack('<Q', compressed_size))
        data_size += 8
    if local_header_offset > ZIP64_SIZE_THRESHOLD:
        fields.append(struct.pack('<Q', local_header_offset))
        data_size += 8
    
    extra = struct.pack('<HH', 0x0001, data_size)
    for field in fields:
        extra += field
    
    return extra


def build_zip64_eocd(num_entries: int, cd_size: int, cd_offset: int) -> bytes:
    """构建 ZIP64 End of Central Directory Record"""
    size = 56  # ZIP64 EOCD 固定大小（不含可变区域）
    
    zip64_eocd = struct.pack('<IQHHIIQQQQ',
        ZIP64_EOCD_SIG,    # 签名
        size - 12,         # EOCD 大小（不含签名和此字段本身的 12 字节）
        45,                # 创建版本
        45,                # 解压版本
        0,                 # 磁盘号
        0,                 # CD 起始磁盘
        num_entries,       # 此磁盘上的条目数
        num_entries,       # 总条目数
        cd_size,           # 中央目录大小
        cd_offset          # 中央目录偏移
    )
    
    return zip64_eocd


def build_zip64_eocd_locator(zip64_eocd_offset: int) -> bytes:
    """构建 ZIP64 EOCD Locator (APPNOTE 4.3.11)。

    字段: 签名(I) + ZIP64 EOCD 所在磁盘号(I) + 偏移(Q) + 总磁盘数(I)。
    """
    return struct.pack('<IIQI',
        ZIP64_EOCD_LOCATOR_SIG,  # 签名
        0,                        # ZIP64 EOCD 所在磁盘号
        zip64_eocd_offset,       # ZIP64 EOCD 的偏移量
        1                         # 总磁盘数
    )


def build_local_header(filename_bytes: bytes, flags: int, method: int,
                       extra_field: bytes = b'',
                       dos_time: int = 0, dos_date: int = 0) -> bytes:
    """
    构建 ZIP 本地文件头
    
    当使用数据描述符时，CRC、压缩大小、未压缩大小设为 0
    dos_time/dos_date 默认 0 (1980-00-00); 传入真实值可让条目显示合理修改时间。
    """
    return struct.pack('<IHHHHHIIIHH',
        LOCAL_HEADER_SIG,       # 签名
        20,                     # 解压版本 (2.0)
        flags,                  # 通用标志位
        method,                 # 压缩方法
        dos_time,               # 修改时间 (DOS)
        dos_date,               # 修改日期 (DOS)
        0,                      # CRC-32 (使用数据描述符时为 0)
        0,                      # 压缩后大小 (使用数据描述符时为 0)
        0,                      # 未压缩大小 (使用数据描述符时为 0)
        len(filename_bytes),    # 文件名长度
        len(extra_field)        # 扩展字段长度
    ) + filename_bytes + extra_field


def build_central_dir_header(filename_bytes: bytes, flags: int, method: int, crc: int,
                             compressed_size: int, uncompressed_size: int,
                             local_header_offset: int, extra_field: bytes = b'',
                             comment: bytes = b'',
                             dos_time: int = 0, dos_date: int = 0) -> bytes:
    """构建 ZIP 中央目录文件头
    
    注意：大小/偏移字段使用 32-bit 阈值 (ZIP64_SIZE_THRESHOLD)，
    不能用 16-bit 的 ZIP64_ENTRIES_THRESHOLD！
    """
    
    # ZIP64 大文件使用版本 45
    version_made = 45 if (compressed_size > ZIP64_SIZE_THRESHOLD or 
                          uncompressed_size > ZIP64_SIZE_THRESHOLD or
                          local_header_offset > ZIP64_SIZE_THRESHOLD) else 20
    version_needed = version_made
    
    # 大小和偏移使用 32-bit 最大值
    comp_size_to_write = compressed_size if compressed_size <= ZIP64_SIZE_THRESHOLD else ZIP64_MARKER
    uncomp_size_to_write = uncompressed_size if uncompressed_size <= ZIP64_SIZE_THRESHOLD else ZIP64_MARKER
    offset_to_write = local_header_offset if local_header_offset <= ZIP64_SIZE_THRESHOLD else ZIP64_MARKER
    
    header = struct.pack('<IHHHHHHIIIHHHHHII',
        CENTRAL_DIR_SIG,        # 签名
        version_made,           # 创建版本
        version_needed,         # 解压版本
        flags,                  # 通用标志位
        method,                 # 压缩方法
        dos_time,               # 修改时间 (DOS)
        dos_date,               # 修改日期 (DOS)
        crc,                    # CRC-32
        comp_size_to_write,     # 压缩后大小 (32-bit)
        uncomp_size_to_write,   # 未压缩大小 (32-bit)
        len(filename_bytes),    # 文件名长度
        len(extra_field),       # 扩展字段长度
        len(comment),           # 文件注释长度
        0,                      # 起始磁盘号
        0,                      # 内部属性
        0,                      # 外部属性
        offset_to_write         # 本地文件头偏移 (32-bit)
    )
    
    return header + filename_bytes + extra_field + comment


def build_eocd(num_entries: int, cd_size: int, cd_offset: int,
               comment: bytes = b'') -> bytes:
    """构建标准 End of Central Directory Record
    
    如果参数超过阈值，使用 ZIP64 占位值
    注意:
      - 条目数量使用 16-bit (阈值 65535)
      - 大小/偏移使用 32-bit (阈值 4294967295)
    """
    # 注释长度限制在 65535 字节内
    comment = comment[:65535]
    
    # 条目数量使用 16-bit 最大值
    entries_to_write = min(num_entries, ZIP64_ENTRIES_THRESHOLD)
    
    # 大小和偏移使用 32-bit 最大值（不能用 16-bit 的 ZIP64_ENTRIES_THRESHOLD！）
    cd_size_to_write = cd_size if cd_size <= ZIP64_SIZE_THRESHOLD else ZIP64_MARKER
    cd_offset_to_write = cd_offset if cd_offset <= ZIP64_SIZE_THRESHOLD else ZIP64_MARKER
    
    return struct.pack('<IHHHHIIH',
        EOCD_SIG,               # 签名
        0,                      # 磁盘号
        0,                      # CD 起始磁盘
        entries_to_write,       # 此磁盘条目数
        entries_to_write,       # 总条目数
        cd_size_to_write,       # 中央目录大小 (32-bit)
        cd_offset_to_write,     # 中央目录偏移 (32-bit)
        len(comment)            # 注释长度
    ) + comment


def build_data_descriptor(crc: int, compressed_size: int,
                          uncompressed_size: int) -> bytes:
    """构建 ZIP 数据描述符（PK\x08\x07 签名版本，WinRAR/7-Zip 推荐）
    
    有签名版本兼容所有工具，无签名版本仅部分工具支持。
    """
    header = 0x08074b50  # 数据描述符签名
    
    if (compressed_size > ZIP64_SIZE_THRESHOLD or 
        uncompressed_size > ZIP64_SIZE_THRESHOLD):
        # ZIP64 数据描述符: signature(4) + CRC-32(4) + compressed(8) + uncompressed(8)
        # 字段顺序与标准分支一致 (APPNOTE 4.3.9), 仅后两个字段扩为 8 字节
        return struct.pack('<IIQQ', header, crc, compressed_size, uncompressed_size)
    else:
        # 标准数据描述符
        return struct.pack('<IIII', header, crc, compressed_size, uncompressed_size)


@contextmanager
def _auto_remove(path: Optional[str]):
    """上下文管理器: 退出时自动删除指定文件 (即使发生异常)。

    用于清理 build_polyglot 中因输出覆盖外层文件而创建的临时副本,
    确保构建中途异常时也不会残留含敏感数据的临时文件。
    """
    try:
        yield
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _cleanup_cancelled_output(output_path: str, outer_path: str,
                              temp_outer: Optional[str]) -> None:
    """构建取消时清理半成品输出。

    - 若输出路径与外层路径相同 (覆盖构建), 尝试从 temp_outer 恢复外层文件;
    - 否则直接删除已生成的半成品输出文件。
    """
    same = os.path.abspath(output_path) == os.path.abspath(outer_path)
    if same:
        if temp_outer and os.path.exists(temp_outer):
            try:
                shutil.copy2(temp_outer, output_path)
            except OSError:
                pass
    else:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass


@contextmanager
def _cancel_scope(output_path: str, outer_path: str, temp_outer: Optional[str]):
    """上下文管理器: 构建取消或被中断时自动恢复/清理半成品输出。

    同时处理 BuildCancelled (GUI stop_event) 与 KeyboardInterrupt (CLI Ctrl+C),
    确保两种取消路径都走同一套清理逻辑。
    """
    try:
        yield
    except (BuildCancelled, KeyboardInterrupt):
        _cleanup_cancelled_output(output_path, outer_path, temp_outer)
        raise


def _stream_copy(f_in: IO[bytes], f_out: IO[bytes], total_size: int,
                 callback: Optional[ProgressCallback],
                 stop_event: Optional[threading.Event],
                 phase: str, label: str,
                 crc_calc: Optional[CRC32Calculator] = None,
                 compressor: Optional['zlib._Compress'] = None) -> tuple[int, int]:
    """通用流式拷贝: 读 chunk -> 可选 CRC -> 可选压缩 -> 写 -> 进度回调 -> 取消检查。

    统一了 build_polyglot 中三段几乎相同的 while 循环
    (复制外层文件 / Deflate 压缩 RAR / Store 直写 RAR)。

    每 0.2s 通过 callback(phase, read, total, f'{label} {read} / {total} ({pct}%)')
    报告进度; 每个 chunk 边界检查 stop_event; 若传入 compressor 则在结束时 flush。

    返回 (bytes_written, bytes_read)。
    """
    written = 0
    read = 0
    # 初始化为 0.0 使首个 chunk 立即上报一次进度 (之后按 0.2s 节流),
    # 既改善 UX (立刻有反馈), 也保证小文件/快速拷贝至少产生一次进度回调。
    last_progress_time = 0.0

    while True:
        _check_stop(stop_event)
        chunk = f_in.read(CHUNK_SIZE)
        if not chunk:
            break
        if crc_calc is not None:
            crc_calc.update(chunk)
        read += len(chunk)
        out_data = compressor.compress(chunk) if compressor is not None else chunk
        if out_data:
            f_out.write(out_data)
            written += len(out_data)
        current_time = time.time()
        if callback and (current_time - last_progress_time > 0.2):
            pct = read * 100 // total_size if total_size > 0 else 0
            callback(phase, read, total_size,
                     f'{label} {format_size(read)} / {format_size(total_size)} '
                     f'({pct}%)')
            last_progress_time = current_time

    # Deflate: flush 剩余压缩数据
    if compressor is not None:
        final = compressor.flush()
        if final:
            f_out.write(final)
            written += len(final)

    return written, read


def build_polyglot(outer_path: str, rar_path: str, output_path: str,
                   callback: Optional[ProgressCallback] = None,
                   method: int = COMP_STORED,
                   stop_event: Optional[threading.Event] = None) -> bool:
    """
    构建 polyglot 文件
    
    参数:
        outer_path: 外层文件路径 (MP4/PDF/JPEG/MP3/BMP 等)
        rar_path: 加密 RAR 文件路径
        output_path: 输出文件路径
        callback: 进度回调函数 callback(phase, current, total, message)
        method: 压缩方法 (COMP_STORED=不压缩[默认], COMP_DEFLATE=Deflate)
        stop_event: 可选 threading.Event; 被设置后抛出 BuildCancelled
    
    返回:
        成功返回 True，被取消抛出 BuildCancelled，其他失败抛出异常
    """
    import tempfile
    
    outer_name = os.path.basename(outer_path)
    rar_name = os.path.basename(rar_path)
    
    # 安全处理: 如果输出路径与外层文件相同，需要先用临时文件
    # 否则 'wb' 打开输出会截断源文件
    outer_source = outer_path
    temp_outer = None
    if os.path.abspath(outer_path) == os.path.abspath(output_path):
        # 创建临时文件保存外层内容
        temp_fd, temp_outer = tempfile.mkstemp(suffix='.tmp', prefix='polyglot_outer_')
        os.close(temp_fd)
        shutil.copy2(outer_path, temp_outer)
        outer_source = temp_outer
        if callback:
            callback('info', 0, 0, '外层文件已复制到临时文件（避免覆盖源文件）')
    
    outer_size = os.path.getsize(outer_source)
    rar_size = os.path.getsize(rar_path)
    
    # === P0: 磁盘空间预检 ===
    output_dir = os.path.dirname(os.path.abspath(output_path))
    disk_free = shutil.disk_usage(output_dir).free
    required_space = outer_size + rar_size + 1024  # ZIP 结构开销
    if disk_free < required_space:
        raise IOError(
            f'磁盘空间不足: 需要 {format_size(required_space)}，'
            f'剩余 {format_size(disk_free)}'
        )
    if callback:
        callback('info', 0, 0, f'磁盘剩余空间: {format_size(disk_free)} (充足)')

    # RAR 有效性 (魔数) 校验: 非标准 RAR 仅警告, 不中断
    rar_warn = _validate_rar(rar_path)
    if rar_warn and callback:
        callback('info', 0, 0, f'⚠ {rar_warn}')

    if callback:
        method_name = 'Store (不压缩)' if method == COMP_STORED else 'Deflate'
        callback('start', 0, rar_size, f'外层文件: {outer_name} ({format_size(outer_size)})')
        callback('start', 0, rar_size, f'RAR 文件: {rar_name} ({format_size(rar_size)})')
        callback('start', 0, rar_size, f'输出文件: {output_path}')
        callback('start', 0, rar_size, f'压缩方式: {method_name}')
        # 输出体积预估 (Store 约等于两者之和; Deflate 取决于压缩率)
        est_note = ('Store, 约等于两者之和' if method == COMP_STORED
                    else 'Deflate, 实际取决于压缩率')
        callback('info', 0, 0,
                 f'预计输出体积: ≈ {format_size(outer_size + rar_size)} ({est_note})')

    # ZIP 条目时间戳: 取 RAR 文件 mtime (无则用当前时间), 避免 1980-00-00 异常特征
    try:
        entry_mtime = os.path.getmtime(rar_path)
    except OSError:
        entry_mtime = time.time()
    dos_time, dos_date = _dos_datetime_from_mtime(entry_mtime)

    # RAR 文件名使用 UTF-8 编码
    filename_bytes = rar_name.encode('utf-8')
    
    # 判断是否需要 ZIP64
    need_zip64 = rar_size > ZIP64_SIZE_THRESHOLD or outer_size > ZIP64_SIZE_THRESHOLD
    
    # ZIP64 扩展字段 (本地文件头)
    # 使用数据描述符时 compressed_size 在写入本地头时尚未确定,
    # 传 0 让 generate_zip64_extra 不写入不确定的 compressed 字段;
    # 真实值最终写在数据描述符和中央目录中。
    # 本地头 extra 不传 offset (offset 字段只属于中央目录 extra),
    # 故第 3 个参数传 0, 确保本地头 extra 只含 uncompressed/compressed。
    zip64_extra = b''
    if need_zip64:
        _local_compressed = rar_size if method == COMP_STORED else 0
        zip64_extra = generate_zip64_extra(rar_size, _local_compressed, 0)
        if callback:
            callback('info', 0, 0, '已启用 ZIP64 扩展（大文件模式）')
    
    # 组合标志位
    flags = FLAG_DATA_DESCRIPTOR | FLAG_UTF8
    
    # 进入长耗时 IO 前检查一次取消
    _check_stop(stop_event)
    
    with _auto_remove(temp_outer), \
         _cancel_scope(output_path, outer_path, temp_outer), \
         open(outer_source, 'rb') as f_outer, \
         open(rar_path, 'rb') as f_rar, \
         open(output_path, 'wb') as f_out:
        
        # ==========================================
        # 第一步: 复制外层文件 (统一走 _stream_copy, 首帧即报 + 0.2s 节流)
        # ==========================================
        if callback:
            callback('copy', 0, outer_size, '正在复制外层文件...')

        _stream_copy(f_outer, f_out, outer_size, callback, stop_event,
                     phase='copy', label='正在复制外层文件...')

        local_header_offset = f_out.tell()
        
        if callback:
            callback('info', 0, 0, 
                    f'外层文件复制完成，ZIP 数据起始偏移: {local_header_offset}')
        
        # ==========================================
        # 第二步: 写入 ZIP 本地文件头
        # ==========================================
        local_header = build_local_header(filename_bytes, flags, method, zip64_extra,
                                          dos_time=dos_time, dos_date=dos_date)
        f_out.write(local_header)
        
        # ==========================================
        # 第三步: 流式处理 RAR 数据
        # ==========================================
        crc_calc = CRC32Calculator()
        if method == COMP_DEFLATE:
            compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
            progress_label = '正在压缩...'
        else:
            # Store 模式: 直接复制，不压缩 (RAR 已高度压缩，Deflate 无收益)
            compressor = None
            progress_label = '正在写入...'

        compressed_size, uncompressed_size = _stream_copy(
            f_rar, f_out, rar_size, callback, stop_event,
            phase='compress', label=progress_label,
            crc_calc=crc_calc, compressor=compressor
        )
        
        if callback:
            if method == COMP_DEFLATE:
                callback('compress', uncompressed_size, rar_size,
                        f'压缩完成: {format_size(uncompressed_size)} → {format_size(compressed_size)} '
                        f'({compressed_size * 100 // uncompressed_size if uncompressed_size > 0 else 0}%)')
            else:
                callback('compress', uncompressed_size, rar_size,
                        f'写入完成: {format_size(uncompressed_size)} (Store 模式，无压缩)')
        
        # ==========================================
        # 第四步: 写入数据描述符
        # ==========================================
        crc_value = crc_calc.value
        data_desc = build_data_descriptor(crc_value, compressed_size, uncompressed_size)
        f_out.write(data_desc)
        
        if callback:
            callback('info', 0, 0, f'CRC-32: {crc_value:08X}')
        
        # ==========================================
        # 第五步: 写入中央目录
        # ==========================================
        central_dir_offset = f_out.tell()
        
        # ZIP64 中央目录扩展字段
        cd_zip64_extra = b''
        if need_zip64:
            cd_zip64_extra = generate_zip64_extra(uncompressed_size, compressed_size, local_header_offset)
        
        central_dir = build_central_dir_header(
            filename_bytes, flags, method,
            crc_value, compressed_size, uncompressed_size,
            local_header_offset, cd_zip64_extra,
            dos_time=dos_time, dos_date=dos_date
        )
        f_out.write(central_dir)
        
        cd_size = f_out.tell() - central_dir_offset
        
        # ==========================================
        # 第六步: 写入 EOCD (和可能的 ZIP64 EOCD)
        # ==========================================
        eocd_data = build_eocd(1, cd_size, central_dir_offset)
        
        if need_zip64:
            zip64_eocd_offset = f_out.tell()
            zip64_eocd = build_zip64_eocd(1, cd_size, central_dir_offset)
            f_out.write(zip64_eocd)
            
            zip64_locator = build_zip64_eocd_locator(zip64_eocd_offset)
            f_out.write(zip64_locator)
        
        # 最后写入标准 EOCD
        f_out.write(eocd_data)
        
        # ==========================================
        # 完成
        # ==========================================
        total_size = f_out.tell()
    
    if callback:
        callback('done', total_size, total_size,
                f'完成! 总输出大小: {format_size(total_size)}')
    

    return True


def verify_polyglot(output_path: str,
                    callback: Optional[ProgressCallback] = None) -> bool:
    """
    验证构建输出的 ZIP 结构完整性
    
    使用 zipfile 流式读取每个条目并增量计算 CRC-32, 与中央目录记录的 CRC 比对。
    分块读取期间通过 callback('verify', read, file_size, ...) 报告进度,
    避免大文件校验时 UI 假死。成功返回 True，失败抛出 IOError。
    """
    if callback:
        callback('info', 0, 0, '正在验证输出文件完整性...')

    try:
        with zipfile.ZipFile(output_path, 'r') as zf:
            names = zf.namelist()
            if not names:
                raise IOError('ZIP 中无文件条目')
            for name in names:
                info = zf.getinfo(name)
                total = info.file_size
                if callback and total > 0:
                    callback('verify', 0, total, f'正在校验 {name}...')
                crc = 0
                read = 0
                try:
                    with zf.open(name) as fp:
                        while True:
                            chunk = fp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            crc = zlib.crc32(chunk, crc)
                            read += len(chunk)
                            if callback and total > 0:
                                callback('verify', read, total,
                                        f'正在校验 {name}... '
                                        f'{format_size(read)} / {format_size(total)}')
                except (zlib.error, OSError) as e:
                    raise IOError(f'读取/解压 {name} 失败: {e}')
                if (crc & 0xFFFFFFFF) != info.CRC:
                    raise IOError(f'CRC 校验失败: {name}')
    except zipfile.BadZipFile as e:
        raise IOError(f'ZIP 结构损坏: {e}')

    if callback:
        callback('info', 0, 0, f'✓ 验证通过 (CRC 正确，包含 {len(names)} 个文件)')
    return True


def progress_callback(phase: str, current: int, total: int, message: str) -> None:
    """简单的命令行进度回调。

    在交互式终端 (TTY) 用 \r 刷新进度条; 输出被重定向到文件/管道 (非 TTY)
    时抑制进度条, 避免产生 \r 乱码, 仅打印阶段信息。
    """
    if phase == 'start':
        print(f'  {message}')
    elif phase == 'info':
        print(f'  {message}')
    elif phase == 'done':
        print(f'  ✓ {message}')
    elif sys.stdout.isatty():
        # 仅交互式终端显示动态进度条
        if total > 0:
            bar_width = 40
            filled = int(bar_width * current / total)
            bar = '█' * filled + '░' * (bar_width - filled)
            percent = current * 100 // total
            print(f'  [{bar}] {percent}%  {message}', end='\r')
        else:
            print(f'  {message}', end='\r')

    # 完成时换行 (仅 TTY 需要; 非 TTY 未打印进度条)
    if sys.stdout.isatty() and phase in ('compress', 'copy') and current >= total:
        print()  # 换行


# ============================================================
# 表面视频压缩 (隐蔽性优化: 让"表面视频"体积更合理, 避免与文件大小违和)
# ============================================================
# 用长视频做外层隐蔽性最好, 但视频原始体积可能过大。
# 可选压缩外层视频, 降低最终文件体积。依赖外部 ffmpeg。

# 3 档压缩质量: key -> (码率 bps, 最大高度, 描述)
VIDEO_QUALITY = {
    'high':  (3_000_000, 1080, '高 (3Mbps, 1080p)'),
    'medium': (1_500_000, 720, '中 (1.5Mbps, 720p, 推荐)'),
    'low':   (800_000, 480, '低 (0.8Mbps, 480p)'),
}
DEFAULT_VIDEO_QUALITY = 'medium'

# ffmpeg 本地缓存目录 (首次压缩时下载到此, 之后复用)
# 存放到程序所在目录下的 ffmpeg/ (源码运行=脚本目录, 打包=exe 目录)
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_LOCAL_DIR = os.path.join(_APP_DIR, 'ffmpeg')

# 下载镜像: (名称, zip 下载 URL)。默认官方, 慢时切换国内镜像。
FFMPEG_MIRRORS = [
    ('官方 gyan.dev',
     'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'),
    ('国内 清华 TUNA',
     'https://mirrors.tuna.tsinghua.edu.cn/github-release/BtbN/FFmpeg-Builds/'
     'latest/ffmpeg-master-latest-win64-gpl-shared.zip'),
    ('国内 中科大 USTC',
     'https://mirrors.ustc.edu.cn/github-release/BtbN/FFmpeg-Builds/'
     'latest/ffmpeg-master-latest-win64-gpl-shared.zip'),
]
DEFAULT_MIRROR_INDEX = 0


def _local_ffmpeg() -> Optional[str]:
    """返回本地缓存目录中已存在的 ffmpeg.exe 路径, 无则 None。"""
    exe = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    # 直接位于 ffmpeg/ 下
    p = os.path.join(FFMPEG_LOCAL_DIR, exe)
    if os.path.isfile(p):
        return p
    # 解压后可能在 ffmpeg/ffmpeg-master-latest-win64-gpl-shared/bin/ 下
    for root, _dirs, files in os.walk(FFMPEG_LOCAL_DIR):
        if exe in files:
            return os.path.join(root, exe)
    return None


def find_ffmpeg() -> Optional[str]:
    """查找可用的 ffmpeg 可执行文件。

    优先查本地缓存目录 (ffmpeg/, 首次压缩时下载到此处),
    再查内置资源目录 (sys._MEIPASS, 打包时随 exe 分发),
    最后回退系统 PATH。找不到返回 None。
    """
    # 1. 本地缓存 (按需下载)
    local = _local_ffmpeg()
    if local:
        return local

    # 2. 内置资源目录 (PyInstaller)
    for base in (getattr(sys, '_MEIPASS', None), _APP_DIR):
        if base:
            p = os.path.join(base, 'ffmpeg', 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
            if os.path.isfile(p):
                return p

    # 3. 系统 PATH
    return shutil.which('ffmpeg')


def _safe_extractall(zf: zipfile.ZipFile, dest: str) -> None:
    """安全解压: 校验每个成员目标路径确实落在 dest 内, 防 Zip Slip。

    含绝对路径或 .. 越界的成员直接跳过 (可信镜像通常无此情况, 属防御性加固)。
    Python 的 ZipFile.extract 自身也会净化路径, 此处再加一层显式校验。
    """
    dest_abs = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != dest_abs and not target.startswith(dest_abs + os.sep):
            continue
        zf.extract(member, dest)


def download_ffmpeg(dest_dir: Optional[str] = None,
                    mirror_index: int = DEFAULT_MIRROR_INDEX,
                    callback: Optional[ProgressCallback] = None,
                    stop_event: Optional[threading.Event] = None) -> str:
    """从指定镜像下载 ffmpeg 并解压到 dest_dir (默认 FFMPEG_LOCAL_DIR)。

    返回下载后 ffmpeg.exe 的路径。下载失败抛 OSError。
    通过 callback('info'/'download', ...) 报告进度。
    """
    if os.name != 'nt':
        raise OSError('当前仅支持在 Windows 上自动下载 ffmpeg。'
                      '其他平台请自行安装 ffmpeg 并加入 PATH。')

    dest = dest_dir or FFMPEG_LOCAL_DIR
    os.makedirs(dest, exist_ok=True)
    if mirror_index < 0 or mirror_index >= len(FFMPEG_MIRRORS):
        raise ValueError(f'镜像索引越界: {mirror_index} (可选 0-{len(FFMPEG_MIRRORS) - 1})')

    name, url = FFMPEG_MIRRORS[mirror_index]
    zip_path = os.path.join(dest, 'ffmpeg_download.zip')

    if callback:
        callback('info', 0, 0, f'正在从 [{name}] 下载 ffmpeg...')

    def _report(cur, total):
        if callback and total > 0:
            callback('download', cur, total,
                     f'下载 ffmpeg... {format_size(cur)} / {format_size(total)}')

    try:
        # 流式下载, 支持进度与取消
        req = urllib.request.Request(url, headers={'User-Agent': 'polyglot-builder'})
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            done = 0
            with open(zip_path, 'wb') as f:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        raise BuildCancelled('ffmpeg 下载已被取消')
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    _report(done, total)
    except urllib.error.URLError as e:
        raise OSError(f'从 [{name}] 下载 ffmpeg 失败: {e}')
    except BuildCancelled:
        raise
    except Exception as e:
        raise OSError(f'下载 ffmpeg 失败: {e}')

    # 解压 (防 Zip Slip: 校验成员路径落在 dest 内)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, dest)
    except Exception as e:
        raise OSError(f'解压 ffmpeg 失败: {e}')
    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

    ffmpeg = _local_ffmpeg()
    if not ffmpeg:
        raise OSError('下载完成但未找到 ffmpeg.exe, 请手动解压检查。')
    if callback:
        callback('info', 0, 0, 'ffmpeg 下载并安装完成')
    return ffmpeg


def _find_ffprobe(ffmpeg_path: str) -> Optional[str]:
    """由 ffmpeg 路径旁推同目录 ffprobe; 不存在则回退系统 PATH, 仍无返回 None。"""
    exe = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
    p = os.path.join(os.path.dirname(ffmpeg_path), exe)
    if os.path.isfile(p):
        return p
    return shutil.which('ffprobe')


def _probe_duration(ffprobe: str, src: str) -> Optional[float]:
    """用 ffprobe 读取媒体时长 (秒); 失败或无法解析返回 None。"""
    try:
        out = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if out.returncode == 0:
            return float(out.stdout.decode('utf-8', 'ignore').strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _parse_ffmpeg_time(val: str) -> Optional[float]:
    """解析 ffmpeg 进度输出的 out_time=HH:MM:SS.micro 为秒; 失败返回 None。"""
    try:
        h, m, s = val.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def _terminate_proc(proc: subprocess.Popen) -> None:
    """终止子进程: 先 terminate 并等待, 超时或异常再 kill。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def compress_video(src: str, dst: str, quality: str = DEFAULT_VIDEO_QUALITY,
                   callback: Optional[ProgressCallback] = None,
                   stop_event: Optional[threading.Event] = None) -> str:
    """用 ffmpeg 压缩视频, 返回压缩后文件路径 dst。

    quality 取值见 VIDEO_QUALITY。ffmpeg 未安装时抛 OSError。
    通过 callback('info', ...) 报告阶段信息; 能探测到源时长时,
    通过 callback('compress', pct, 100, ...) 报告编码进度百分比。

    实现要点:
      - `-progress pipe:1 -nostats`: ffmpeg 把 key=value 进度写到 stdout, 按行解析
        out_time 换算百分比;
      - stderr 重定向到临时文件而非管道: 长编码时避免 stderr 管道缓冲区写满
        导致 ffmpeg 阻塞 (死锁), 失败时读尾部 500 字符报错;
      - 读取每行间隙检查 stop_event, 支持取消 (terminate/kill)。
    """
    import tempfile

    if quality not in VIDEO_QUALITY:
        raise ValueError(f'未知压缩档位: {quality} (可选 {list(VIDEO_QUALITY)})')

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise OSError(
            '未找到 ffmpeg。请安装 ffmpeg 并加入 PATH, '
            '或使用打包版 (已内置 ffmpeg)。')

    bitrate, max_height, _label = VIDEO_QUALITY[quality]
    if callback:
        callback('info', 0, 0,
                 f'正在压缩表面视频 ({quality}, {bitrate // 1000}kbps)...')

    # 探测源时长用于换算进度百分比 (缺 ffprobe/探测失败时降级为无百分比)
    ffprobe = _find_ffprobe(ffmpeg)
    duration = _probe_duration(ffprobe, src) if ffprobe else None

    # 滤镜: 仅当原始高度高于 max_height 时按比例缩到该高度, 否则保持原尺寸
    # scale 宽: 高>max_height 时按比例(-2 保持偶数), 否则保持原宽(iw)
    vf = (f"scale='if(gt(ih\\,{max_height})\\,trunc(iw*{max_height}/ih/2)*2\\,iw)':"
          f"'if(gt(ih\\,{max_height})\\,{max_height}\\,ih)'")

    cmd = [
        ffmpeg, '-y', '-nostats', '-i', src,
        '-c:v', 'libx264', '-preset', 'medium',
        '-b:v', str(bitrate), '-maxrate', str(int(bitrate * 1.2)),
        '-bufsize', str(int(bitrate * 2)), '-vf', vf,
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        '-progress', 'pipe:1',
        dst,
    ]

    err_fd, err_path = tempfile.mkstemp(suffix='.log', prefix='polyglot_ffmpeg_')
    os.close(err_fd)
    proc: Optional[subprocess.Popen] = None
    try:
        with open(err_path, 'wb') as err_file:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file)
            assert proc.stdout is not None
            # 按行读取 -progress 输出; 每行都检查取消
            for raw in proc.stdout:
                if stop_event is not None and stop_event.is_set():
                    _terminate_proc(proc)
                    raise BuildCancelled('压缩已被用户取消')
                line = raw.decode('utf-8', 'ignore').strip()
                if not line or '=' not in line:
                    continue
                key, _sep, val = line.partition('=')
                if key == 'out_time' and duration and duration > 0 and callback:
                    elapsed = _parse_ffmpeg_time(val)
                    if elapsed is not None:
                        pct = min(100, int(elapsed * 100 / duration))
                        callback('compress', pct, 100,
                                 f'正在压缩表面视频... {pct}%')
            proc.wait()
        if proc.returncode != 0:
            stderr_tail = ''
            try:
                with open(err_path, 'rb') as f:
                    stderr_tail = f.read().decode('utf-8', 'ignore')[-500:]
            except OSError:
                pass
            raise OSError(
                f'ffmpeg 压缩失败 (退出码 {proc.returncode}):\n{stderr_tail}')
    except BuildCancelled:
        raise
    except OSError:
        raise
    except Exception as e:
        if proc is not None:
            _terminate_proc(proc)
        raise OSError(f'ffmpeg 执行失败: {e}')
    finally:
        if os.path.exists(err_path):
            try:
                os.remove(err_path)
            except OSError:
                pass

    if callback:
        callback('info', 0, 0, '表面视频压缩完成')
    return dst


def _temp_path_for_outer(outer_path: str) -> str:
    """为压缩外层生成临时输出路径 (与源同目录, 前缀 polyglot_compressed_)。"""
    d = os.path.dirname(os.path.abspath(outer_path))
    base = os.path.splitext(os.path.basename(outer_path))[0]
    return os.path.join(d, f'polyglot_compressed_{base}.mp4')


def _parse_batch_manifest(path: str) -> List[Tuple[str, str, str]]:
    """解析批量清单文件, 返回 (outer, rar, output) 三元组列表。

    每行格式: outer|rar[|output]
      - 以 '|' 分隔; 第三段 output 可选, 缺省时输出与 outer 同名;
      - 忽略空行与以 '#' 开头的注释行; 各段自动 strip 去首尾空白。
    文件不存在抛 IOError; 字段不足 2 个或关键字段为空的行抛 ValueError。
    """
    if not os.path.isfile(path):
        raise IOError(f'批量清单文件不存在: {path}')
    tasks: List[Tuple[str, str, str]] = []
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f'清单第 {lineno} 行格式非法 (需 outer|rar[|output]): {line}')
            outer, rar = parts[0], parts[1]
            output = parts[2] if len(parts) >= 3 and parts[2] else outer
            tasks.append((outer, rar, output))
    return tasks


def _run_batch(args: argparse.Namespace, logger: logging.Logger) -> None:
    """批量模式: 逐条构建清单任务并汇总成败。

    复用单文件的压缩/构建/校验流程; 任一条失败不中断后续 (Ctrl+C 除外),
    末尾统计成功/失败数。全部成功 sys.exit(0), 存在失败 sys.exit(1)。
    """
    try:
        tasks = _parse_batch_manifest(args.batch)
    except (IOError, ValueError) as e:
        print(f'错误: {e}', file=sys.stderr)
        sys.exit(1)

    if not tasks:
        print('错误: 批量清单为空, 无任务可执行', file=sys.stderr)
        sys.exit(1)

    callback = None if args.quiet else progress_callback
    method = COMP_DEFLATE if args.deflate else COMP_STORED

    logger.info('Polyglot Builder v%s - 批量模式 (%d 个任务)', VERSION, len(tasks))
    logger.info('=' * 40)

    ok = 0
    failed: List[str] = []
    for idx, (outer, rar, output) in enumerate(tasks, 1):
        logger.info('[%d/%d] 处理: %s', idx, len(tasks), output)
        compressed_outer: Optional[str] = None
        try:
            if not os.path.isfile(outer):
                raise IOError(f'外层文件不存在: {outer}')
            if not os.path.isfile(rar):
                raise IOError(f'RAR 文件不存在: {rar}')

            effective_outer = outer
            if args.compress:
                compressed_outer = _temp_path_for_outer(outer)
                compress_video(outer, compressed_outer,
                               quality=args.compress, callback=callback)
                effective_outer = compressed_outer

            if callback:
                callback('start', 0, 0, f'外层文件: {outer}')
                callback('start', 0, 0, f'RAR 文件: {rar}')
                callback('start', 0, 0, f'输出文件: {output}')

            build_polyglot(effective_outer, rar, output, callback, method=method)

            if not args.no_verify:
                verify_polyglot(output, callback)

            ok += 1
            logger.info('[%d/%d] ✓ 完成: %s', idx, len(tasks), output)
        except Exception as e:  # 单条失败不阻断整批
            failed.append(f'{outer} -> {output}: {e}')
            logger.error('[%d/%d] ✗ 失败: %s', idx, len(tasks), e)
        finally:
            if compressed_outer and os.path.exists(compressed_outer):
                try:
                    os.remove(compressed_outer)
                except OSError:
                    pass

    logger.info('=' * 40)
    logger.info('批量完成: 成功 %d, 失败 %d (共 %d)', ok, len(failed), len(tasks))
    if failed:
        for item in failed:
            logger.error('  失败项: %s', item)
        sys.exit(1)
    sys.exit(0)


def main() -> None:
    """主函数 - 解析命令行参数并执行构建"""
    parser = argparse.ArgumentParser(
        description='Polyglot Builder - 将媒体文件与加密 RAR 拼接为多格式文件',
        epilog='示例:\n'
               '  python polyglot_build.py video.mp4 game.rar -o output.mp4\n'
               '  python polyglot_build.py photo.jpg secret.rar -y --deflate\n'
               '  python polyglot_build.py --gui',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        'outer_file',
        nargs='?',
        help='外层文件路径 (MP4/PDF/JPEG/MP3/BMP 等)'
    )
    
    parser.add_argument(
        'rar_file',
        nargs='?',
        help='加密 RAR 压缩包路径 (需先 WinRAR 创建并设置密码)'
    )
    
    parser.add_argument(
        '-o', '--output',
        help='输出文件路径 (默认: 与外层文件同名)'
    )
    
    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='静默模式，不显示进度'
    )
    
    parser.add_argument(
        '--deflate',
        action='store_true',
        help='使用 Deflate 压缩 (默认 Store 不压缩，RAR 已压缩无需再压)'
    )
    
    parser.add_argument(
        '--no-verify',
        action='store_true',
        help='跳过构建后 ZIP 完整性验证'
    )

    parser.add_argument(
        '-y', '--force',
        action='store_true',
        help='强制覆盖已存在的输出文件, 不交互询问 (适合脚本/CI)'
    )

    parser.add_argument(
        '--compress',
        nargs='?',
        const=DEFAULT_VIDEO_QUALITY,
        choices=list(VIDEO_QUALITY),
        metavar='QUALITY',
        help='压缩外层视频以减小最终文件体积 (提高隐蔽性)。'
             f'QUALITY: {", ".join(VIDEO_QUALITY)} (默认 {DEFAULT_VIDEO_QUALITY})。'
             '需安装 ffmpeg 或使用打包版'
    )

    parser.add_argument(
        '--gui',
        action='store_true',
        help='启动图形界面 (GUI 模式)'
    )

    parser.add_argument(
        '--batch',
        metavar='MANIFEST',
        help='批量模式: 指定清单文本文件, 每行 "外层|RAR[|输出]"; '
             '忽略空行与 # 注释行。任一条失败不中断后续, 末尾汇总成败'
    )

    parser.add_argument(
        '--log-file',
        metavar='PATH',
        help='将日志额外持久化到指定文件 (追加模式, UTF-8), 便于事后排查'
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'Polyglot Builder v{VERSION}'
    )

    args = parser.parse_args()

    # 统一日志: CLI 与 GUI 共用
    level = logging.WARNING if args.quiet else logging.INFO
    log = setup_logging(level, log_file=args.log_file)
    logger = get_logger()

    # 无参数运行 (双击打包版 exe / 直接运行脚本) → 自动进入 GUI,
    # 与 polyglot_build.bat 的"双模式"(有参走 CLI, 无参走 GUI) 一致。
    # 仅在既无位置参数又无 --batch 时回退 GUI; 只给部分位置参数仍按 CLI 报错。
    bare_launch = (not args.gui and not args.batch
                   and not args.outer_file and not args.rar_file)
    if bare_launch:
        args.gui = True

    # 如果使用 --gui 参数，启动图形界面 (在参数验证之前)
    if args.gui:
        try:
            import tkinter as tk
            from polyglot_gui import launch_gui
            launch_gui()
            sys.exit(0)
        except ImportError:
            print('错误: 无法加载 GUI 模块，请确保 polyglot_gui.py 在同一目录', file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f'错误: 无法启动图形界面: {e}', file=sys.stderr)
            sys.exit(1)
    
    # 批量模式: 从清单读取多条任务, 在单文件参数验证之前处理
    if args.batch:
        _run_batch(args, logger)
        return

    # 以下仅在非 GUI 模式下验证
    if not args.outer_file:
        parser.error('外层文件路径是必需的 (或使用 --gui / --batch)')
    if not args.rar_file:
        parser.error('RAR 文件路径是必需的 (或使用 --gui / --batch)')
    
    # 检查外层文件
    if not os.path.isfile(args.outer_file):
        print(f'错误: 外层文件不存在: {args.outer_file}', file=sys.stderr)
        sys.exit(1)
    
    # 检查 RAR 文件
    if not os.path.isfile(args.rar_file):
        print(f'错误: RAR 文件不存在: {args.rar_file}', file=sys.stderr)
        sys.exit(1)
    
    # 确定输出路径
    if args.output:
        output_path = args.output
    else:
        # 默认命名: 与外层文件同名
        output_path = args.outer_file
    
    # 检查输出目录是否存在
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.isdir(output_dir):
        print(f'错误: 输出目录不存在: {output_dir}', file=sys.stderr)
        sys.exit(1)
    
    # 检查是否会覆盖
    if os.path.exists(output_path):
        if args.quiet or args.force:
            # 静默/强制模式: 自动覆盖，不询问
            pass
        else:
            print(f'警告: 输出文件已存在，将覆盖: {output_path}')
            response = input('是否继续? (y/N): ').strip().lower()
            if response != 'y':
                print('已取消')
                sys.exit(0)
    
    # 选择回调
    callback = None if args.quiet else progress_callback
    
    # 确定压缩方法
    method = COMP_DEFLATE if args.deflate else COMP_STORED

    # 表面视频压缩: 先压缩外层, 再用压缩产物拼接
    effective_outer: str = args.outer_file
    compressed_outer: Optional[str] = None
    try:
        if args.compress:
            compressed_outer = _temp_path_for_outer(args.outer_file)
            compress_video(args.outer_file, compressed_outer,
                           quality=args.compress, callback=callback)
            effective_outer = compressed_outer

        # 开始构建
        logger.info('Polyglot Builder v%s', VERSION)
        logger.info('=' * 40)

        if callback:
            callback('start', 0, 0, f'外层文件: {args.outer_file}')
            callback('start', 0, 0, f'RAR 文件: {args.rar_file}')
            callback('start', 0, 0, f'输出文件: {output_path}')

        build_polyglot(effective_outer, args.rar_file, output_path, callback, method=method)

        # 构建后验证
        if not args.no_verify:
            verify_polyglot(output_path, callback)

        logger.info('=' * 40)
        logger.info('完成! 输出文件: %s', output_path)
        logger.info('使用方式:')
        logger.info('  1. 直接在播放器/查看器中打开 → 显示外层内容')
        logger.info('  2. 改后缀名为 .zip → 用 WinRAR/7-Zip 打开')
        logger.info('  3. 解压后得到 RAR 文件 → 输入密码解压')

        # 清理压缩外层临时文件
        if compressed_outer and os.path.exists(compressed_outer):
            try:
                os.remove(compressed_outer)
            except OSError:
                pass

    except KeyboardInterrupt:
        if compressed_outer and os.path.exists(compressed_outer):
            try:
                os.remove(compressed_outer)
            except OSError:
                pass
        print('\n已取消 (Ctrl+C), 已清理半成品输出', file=sys.stderr)
        sys.exit(130)
    except BuildCancelled:
        if compressed_outer and os.path.exists(compressed_outer):
            try:
                os.remove(compressed_outer)
            except OSError:
                pass
        print('\n已取消, 已清理半成品输出', file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        if compressed_outer and os.path.exists(compressed_outer):
            try:
                os.remove(compressed_outer)
            except OSError:
                pass
        print(f'错误: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
