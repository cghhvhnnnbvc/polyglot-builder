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

作者: CatPaw (美团)
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
from typing import Callable, List, Optional

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


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置全局日志 (幂等)。CLI 与 GUI 共用此入口, 保证输出格式一致。

    默认只在根 logger 挂一个带格式的 StreamHandler (stdout)。
    """
    logger = get_logger()
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter('%(message)s'))  # 简洁: 仅消息本身
        logger.addHandler(handler)
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


class CRC32Calculator:
    """增量式 CRC-32 计算器，用于大文件流式处理"""
    
    def __init__(self) -> None:
        self.crc = 0
    
    def update(self, data: bytes) -> None:
        self.crc = zlib.crc32(data, self.crc)
    
    @property
    def value(self) -> int:
        return self.crc & 0xFFFFFFFF


def format_size(size_bytes):
    """将字节数格式化为人类可读形式"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


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


def build_zip64_eocd(num_entries, cd_size, cd_offset):
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
                       extra_field: bytes = b'') -> bytes:
    """
    构建 ZIP 本地文件头
    
    当使用数据描述符时，CRC、压缩大小、未压缩大小设为 0
    """
    return struct.pack('<IHHHHHIIIHH',
        LOCAL_HEADER_SIG,       # 签名
        20,                     # 解压版本 (2.0)
        flags,                  # 通用标志位
        method,                 # 压缩方法
        0,                      # 修改时间 (DOS)
        0,                      # 修改日期 (DOS)
        0,                      # CRC-32 (使用数据描述符时为 0)
        0,                      # 压缩后大小 (使用数据描述符时为 0)
        0,                      # 未压缩大小 (使用数据描述符时为 0)
        len(filename_bytes),    # 文件名长度
        len(extra_field)        # 扩展字段长度
    ) + filename_bytes + extra_field


def build_central_dir_header(filename_bytes: bytes, flags: int, method: int, crc: int,
                             compressed_size: int, uncompressed_size: int,
                             local_header_offset: int, extra_field: bytes = b'',
                             comment: bytes = b'') -> bytes:
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
        0,                      # 修改时间 (DOS)
        0,                      # 修改日期 (DOS)
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


def _stream_copy(f_in, f_out, total_size: int,
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
    last_progress_time = time.time()

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
    
    if callback:
        method_name = 'Store (不压缩)' if method == COMP_STORED else 'Deflate'
        callback('start', 0, rar_size, f'外层文件: {outer_name} ({format_size(outer_size)})')
        callback('start', 0, rar_size, f'RAR 文件: {rar_name} ({format_size(rar_size)})')
        callback('start', 0, rar_size, f'输出文件: {output_path}')
        callback('start', 0, rar_size, f'压缩方式: {method_name}')
    
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
        # 第一步: 复制外层文件
        # ==========================================
        if callback:
            callback('copy', 0, outer_size, f'正在复制外层文件...')

        copied = 0
        while True:
            _check_stop(stop_event)
            chunk = f_outer.read(CHUNK_SIZE)
            if not chunk:
                break
            f_out.write(chunk)
            copied += len(chunk)
            if callback:
                callback('copy', copied, outer_size,
                        f'正在复制外层文件... {format_size(copied)} / {format_size(outer_size)}')

        local_header_offset = f_out.tell()
        
        if callback:
            callback('info', 0, 0, 
                    f'外层文件复制完成，ZIP 数据起始偏移: {local_header_offset}')
        
        # ==========================================
        # 第二步: 写入 ZIP 本地文件头
        # ==========================================
        local_header = build_local_header(filename_bytes, flags, method, zip64_extra)
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
            local_header_offset, cd_zip64_extra
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


def ensure_ffmpeg() -> Optional[str]:
    """确保 ffmpeg 可用。已安装则返回其路径, 否则返回 None (需下载)。

    与 find_ffmpeg 的区别: ensure_ffmpeg 不主动下载, 只负责检测;
    调用方据此决定是否提示用户下载。
    """
    return find_ffmpeg()


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

    # 解压
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
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


def compress_video(src: str, dst: str, quality: str = DEFAULT_VIDEO_QUALITY,
                   callback: Optional[ProgressCallback] = None,
                   stop_event: Optional[threading.Event] = None) -> str:
    """用 ffmpeg 压缩视频, 返回压缩后文件路径 dst。

    quality 取值见 VIDEO_QUALITY。ffmpeg 未安装时抛 OSError。
    通过 callback('info', ...) 报告阶段信息。
    """
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

    # 滤镜: 仅当原始高度高于 max_height 时按比例缩到该高度, 否则保持原尺寸
    # scale 宽: 高>max_height 时按比例(-2 保持偶数), 否则保持原宽(iw)
    vf = (f"scale='if(gt(ih\\,{max_height})\\,trunc(iw*{max_height}/ih/2)*2\\,iw)':"
          f"'if(gt(ih\\,{max_height})\\,{max_height}\\,ih)'")

    cmd = [
        ffmpeg, '-y', '-i', src,
        '-c:v', 'libx264', '-preset', 'medium',
        '-b:v', str(bitrate), '-maxrate', str(int(bitrate * 1.2)),
        '-bufsize', str(int(bitrate * 2)), '-vf', vf,
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        dst,
    ]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # 等待完成, 期间检查取消
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                raise BuildCancelled('压缩已被用户取消')
            time.sleep(0.1)
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode('utf-8', 'ignore') if proc.stderr else ''
            raise OSError(f'ffmpeg 压缩失败 (退出码 {proc.returncode}):\n{stderr[-500:]}')
    except BuildCancelled:
        raise
    except OSError:
        raise
    except Exception as e:
        raise OSError(f'ffmpeg 执行失败: {e}')

    if callback:
        callback('info', 0, 0, '表面视频压缩完成')
    return dst


def _temp_path_for_outer(outer_path: str) -> str:
    """为压缩外层生成临时输出路径 (与源同目录, 前缀 polyglot_compressed_)。"""
    d = os.path.dirname(os.path.abspath(outer_path))
    base = os.path.splitext(os.path.basename(outer_path))[0]
    return os.path.join(d, f'polyglot_compressed_{base}.mp4')


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
        '--version',
        action='version',
        version=f'Polyglot Builder v{VERSION}'
    )

    args = parser.parse_args()

    # 统一日志: CLI 与 GUI 共用
    level = logging.WARNING if args.quiet else logging.INFO
    log = setup_logging(level)
    logger = get_logger()

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
    
    # 以下仅在非 GUI 模式下验证
    if not args.outer_file:
        parser.error('外层文件路径是必需的 (或使用 --gui 启动图形界面)')
    if not args.rar_file:
        parser.error('RAR 文件路径是必需的 (或使用 --gui 启动图形界面)')
    
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
