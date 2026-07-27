#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import zipfile
from contextlib import contextmanager

# ============================================================
# 版本号 (单一来源: GUI / CLI / bat 均需与此保持一致)
# ============================================================
VERSION = '3.0'


class BuildCancelled(Exception):
    """构建被用户取消时抛出的异常。"""


def _check_stop(stop_event):
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
ZIP64_CD_THRESHOLD = 0xFFFFFFFF    # 中央目录大小/偏移超过此值
ZIP64_ENTRIES_THRESHOLD = 0xFFFF    # 条目数超过 65535


class CRC32Calculator:
    """增量式 CRC-32 计算器，用于大文件流式处理"""
    
    def __init__(self):
        self.crc = 0
    
    def update(self, data):
        self.crc = zlib.crc32(data, self.crc)
    
    @property
    def value(self):
        return self.crc & 0xFFFFFFFF


def format_size(size_bytes):
    """将字节数格式化为人类可读形式"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def generate_zip64_extra(uncompressed_size, compressed_size, local_header_offset):
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


def build_zip64_eocd_locator(zip64_eocd_offset):
    """构建 ZIP64 EOCD Locator"""
    return struct.pack('<IQI',
        ZIP64_EOCD_LOCATOR_SIG,  # 签名
        0,                        # ZIP64 EOCD 所在磁盘号
        zip64_eocd_offset,       # ZIP64 EOCD 的偏移量
        1                         # 总磁盘数
    )


def build_local_header(filename_bytes, flags, method, extra_field=b''):
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


def build_central_dir_header(filename_bytes, flags, method, crc, 
                             compressed_size, uncompressed_size, 
                             local_header_offset, extra_field=b'', comment=b''):
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
    comp_size_to_write = compressed_size if compressed_size <= ZIP64_SIZE_THRESHOLD else ZIP64_SIZE_THRESHOLD
    uncomp_size_to_write = uncompressed_size if uncompressed_size <= ZIP64_SIZE_THRESHOLD else ZIP64_SIZE_THRESHOLD
    offset_to_write = local_header_offset if local_header_offset <= ZIP64_SIZE_THRESHOLD else ZIP64_SIZE_THRESHOLD
    
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


def build_eocd(num_entries, cd_size, cd_offset, comment=b''):
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
    cd_size_to_write = cd_size if cd_size <= ZIP64_SIZE_THRESHOLD else ZIP64_SIZE_THRESHOLD
    cd_offset_to_write = cd_offset if cd_offset <= ZIP64_SIZE_THRESHOLD else ZIP64_SIZE_THRESHOLD
    
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


def build_data_descriptor(crc, compressed_size, uncompressed_size):
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
def _auto_remove(path):
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


def _cleanup_cancelled_output(output_path, outer_path, temp_outer):
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
def _cancel_scope(output_path, outer_path, temp_outer):
    """上下文管理器: 构建取消或被中断时自动恢复/清理半成品输出。

    同时处理 BuildCancelled (GUI stop_event) 与 KeyboardInterrupt (CLI Ctrl+C),
    确保两种取消路径都走同一套清理逻辑。
    """
    try:
        yield
    except (BuildCancelled, KeyboardInterrupt):
        _cleanup_cancelled_output(output_path, outer_path, temp_outer)
        raise


def build_polyglot(outer_path, rar_path, output_path, callback=None,
                   method=COMP_STORED, stop_event=None):
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
    zip64_extra = b''
    if need_zip64:
        _local_compressed = rar_size if method == COMP_STORED else 0
        zip64_extra = generate_zip64_extra(rar_size, _local_compressed, outer_size)
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
        compressed_size = 0
        uncompressed_size = 0
        
        if method == COMP_DEFLATE:
            # Deflate 压缩模式
            compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
            
            last_progress_time = time.time()
            while True:
                _check_stop(stop_event)
                chunk = f_rar.read(CHUNK_SIZE)
                if not chunk:
                    break
                crc_calc.update(chunk)
                uncompressed_size += len(chunk)
                compressed_chunk = compressor.compress(chunk)
                if compressed_chunk:
                    f_out.write(compressed_chunk)
                    compressed_size += len(compressed_chunk)
                current_time = time.time()
                if callback and (current_time - last_progress_time > 0.2):
                    callback('compress', uncompressed_size, rar_size,
                            f'正在压缩... {format_size(uncompressed_size)} / {format_size(rar_size)} '
                            f'({uncompressed_size * 100 // rar_size if rar_size > 0 else 0}%)')
                    last_progress_time = current_time
            
            final_compressed = compressor.flush()
            if final_compressed:
                f_out.write(final_compressed)
                compressed_size += len(final_compressed)
        else:
            # Store 模式: 直接复制，不压缩 (RAR 已高度压缩，Deflate 无收益)
            last_progress_time = time.time()
            while True:
                _check_stop(stop_event)
                chunk = f_rar.read(CHUNK_SIZE)
                if not chunk:
                    break
                crc_calc.update(chunk)
                uncompressed_size += len(chunk)
                f_out.write(chunk)
                compressed_size += len(chunk)
                current_time = time.time()
                if callback and (current_time - last_progress_time > 0.2):
                    callback('compress', uncompressed_size, rar_size,
                            f'正在写入... {format_size(uncompressed_size)} / {format_size(rar_size)} '
                            f'({uncompressed_size * 100 // rar_size if rar_size > 0 else 0}%)')
                    last_progress_time = current_time
        
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


def verify_polyglot(output_path, callback=None):
    """
    验证构建输出的 ZIP 结构完整性
    
    使用 zipfile 模块打开输出文件并校验 CRC。
    成功返回 True，失败抛出异常。
    """
    if callback:
        callback('info', 0, 0, '正在验证输出文件完整性...')
    
    try:
        with zipfile.ZipFile(output_path, 'r') as zf:
            bad_file = zf.testzip()
            if bad_file is not None:
                raise IOError(f'CRC 校验失败: {bad_file}')
            names = zf.namelist()
            if not names:
                raise IOError('ZIP 中无文件条目')
    except zipfile.BadZipFile as e:
        raise IOError(f'ZIP 结构损坏: {e}')
    
    if callback:
        callback('info', 0, 0, f'✓ 验证通过 (CRC 正确，包含 {len(names)} 个文件)')
    return True


def progress_callback(phase, current, total, message):
    """简单的命令行进度回调"""
    if phase == 'start':
        print(f'  {message}')
    elif phase == 'info':
        print(f'  {message}')
    elif phase == 'done':
        print(f'  ✓ {message}')
    else:
        # 进度条
        if total > 0:
            bar_width = 40
            filled = int(bar_width * current / total)
            bar = '█' * filled + '░' * (bar_width - filled)
            percent = current * 100 // total
            print(f'  [{bar}] {percent}%  {message}', end='\r')
        else:
            print(f'  {message}', end='\r')
    
    # 完成时换行
    if phase in ('compress', 'copy') and current >= total:
        print()  # 换行


def main():
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
    
    try:
        # 开始构建
        print(f'Polyglot Builder v{VERSION}')
        print(f'========================================')
        
        if callback:
            callback('start', 0, 0, f'外层文件: {args.outer_file}')
            callback('start', 0, 0, f'RAR 文件: {args.rar_file}')
            callback('start', 0, 0, f'输出文件: {output_path}')
        
        build_polyglot(args.outer_file, args.rar_file, output_path, callback, method=method)
        
        # 构建后验证
        if not args.no_verify:
            verify_polyglot(output_path, callback)
        
        print(f'========================================')
        print(f'完成! 输出文件: {output_path}')
        print(f'使用方式:')
        print(f'  1. 直接在播放器/查看器中打开 → 显示外层内容')
        print(f'  2. 改后缀名为 .zip → 用 WinRAR/7-Zip 打开')
        print(f'  3. 解压后得到 RAR 文件 → 输入密码解压')
        
    except KeyboardInterrupt:
        print('\n已取消 (Ctrl+C), 已清理半成品输出', file=sys.stderr)
        sys.exit(130)
    except BuildCancelled:
        print('\n已取消, 已清理半成品输出', file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f'错误: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
