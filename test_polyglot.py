# -*- coding: utf-8 -*-
"""Polyglot Builder 回归测试 (标准库 unittest, 零依赖)。

守护 P0 修复:
  1. build_data_descriptor 的 ZIP64 分支字段顺序必须为
     signature -> CRC-32 -> compressed -> uncompressed (<IIQQ)。
  2. 端到端构建 (Store 模式) 生成的文件能被 zipfile 校验通过且内容还原。
  3. 输出覆盖外层文件时, 临时副本在构建完成后被清理。
"""

import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polyglot_build
from polyglot_build import (
    build_data_descriptor, build_polyglot, verify_polyglot,
    generate_zip64_extra, progress_callback,
    get_logger, setup_logging, compress_video, find_ffmpeg, VIDEO_QUALITY,
    COMP_STORED, COMP_DEFLATE, ZIP64_SIZE_THRESHOLD, BuildCancelled,
)
from polyglot_gui import get_output_save_options, _should_follow_outer, PolyglotGUI


class TestDataDescriptor(unittest.TestCase):
    def test_zip64_descriptor_field_order(self):
        # 触发 ZIP64 分支 (>4GB)
        crc = 0xDEADBEEF
        comp = ZIP64_SIZE_THRESHOLD + 1
        uncomp = ZIP64_SIZE_THRESHOLD + 10
        data = build_data_descriptor(crc, comp, uncomp)
        sig, crc32, comp64, uncomp64 = struct.unpack('<IIQQ', data)
        self.assertEqual(sig, 0x08074b50)
        self.assertEqual(crc32, crc)
        self.assertEqual(comp64, comp)
        self.assertEqual(uncomp64, uncomp)

    def test_standard_descriptor_field_order(self):
        crc = 0x11223344
        comp = 100
        uncomp = 200
        data = build_data_descriptor(crc, comp, uncomp)
        sig, crc32, comp32, uncomp32 = struct.unpack('<IIII', data)
        self.assertEqual(sig, 0x08074b50)
        self.assertEqual(crc32, crc)
        self.assertEqual(comp32, comp)
        self.assertEqual(uncomp32, uncomp)


class TestZip64Extra(unittest.TestCase):
    """守护 A1: ZIP64 extra 字段按需包含, 本地头不误含 offset。"""

    def test_local_header_extra_omits_offset(self):
        # 本地头 extra 不传 offset (传 0), 故只含 uncompressed + compressed
        big = ZIP64_SIZE_THRESHOLD + 1
        extra = generate_zip64_extra(big, big, 0)
        field_id, data_size = struct.unpack('<HH', extra[:4])
        self.assertEqual(field_id, 0x0001)
        self.assertEqual(data_size, 16)  # uncompressed(8) + compressed(8)

    def test_central_dir_extra_includes_offset(self):
        # 中央目录 extra 传真实 offset, 应额外含 offset 字段
        big = ZIP64_SIZE_THRESHOLD + 1
        extra = generate_zip64_extra(big, big, big)
        _id, data_size = struct.unpack('<HH', extra[:4])
        self.assertEqual(data_size, 24)  # + offset(8)

    def test_fields_omitted_when_below_threshold(self):
        # 低于阈值的字段不写入 extra
        extra = generate_zip64_extra(100, 100, 100)
        _id, data_size = struct.unpack('<HH', extra[:4])
        self.assertEqual(data_size, 0)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='polyglot_test_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name, data):
        p = os.path.join(self.tmpdir, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def _temp_outer_leftovers(self):
        tmp = tempfile.gettempdir()
        return {f for f in os.listdir(tmp) if f.startswith('polyglot_outer_')}

    def test_store_mode_roundtrip(self):
        outer = self._make_file('outer.bin', b'OUTER_HEADER' + b'\x00' * 1024)
        payload = b'SECRET_RAR_CONTENT' * 100
        rar = self._make_file('secret.rar', payload)
        out = os.path.join(self.tmpdir, 'poly.bin')

        build_polyglot(outer, rar, out, method=COMP_STORED)
        verify_polyglot(out)

        with zipfile.ZipFile(out) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(zf.read(zf.namelist()[0]), payload)

    def test_temp_cleanup_on_overwrite(self):
        before = self._temp_outer_leftovers()
        outer = self._make_file('over.bin', b'HEADER' + b'\x00' * 512)
        rar = self._make_file('s.rar', b'RAR' * 50)

        # 输出路径 == 外层路径, 触发临时副本机制
        build_polyglot(outer, rar, outer, method=COMP_STORED)

        after = self._temp_outer_leftovers()
        self.assertEqual(after - before, set())

    def test_deflate_mode_roundtrip(self):
        # 3.1: Deflate 模式端到端, 验证压缩生效且内容/CRC 正确
        outer = self._make_file('outer.bin', b'OUTER_HEADER' + b'\x00' * 1024)
        payload = b'SECRET_RAR_CONTENT' * 100
        rar = self._make_file('secret.rar', payload)
        out = os.path.join(self.tmpdir, 'poly_def.bin')

        build_polyglot(outer, rar, out, method=COMP_DEFLATE)
        verify_polyglot(out)

        with zipfile.ZipFile(out) as zf:
            self.assertIsNone(zf.testzip())
            self.assertEqual(zf.read(zf.namelist()[0]), payload)
            info = zf.infolist()[0]
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            self.assertLess(info.compress_size, info.file_size,
                            'Deflate 应实际压缩数据')

    def test_zip64_boundary_roundtrip(self):
        # 3.2: 把 ZIP64 阈值调小, 用小文件触发完整 ZIP64 路径 (Store)
        # 守护 P0#1 (ZIP64 数据描述符字段顺序) 与本地头/中央目录 ZIP64 extra
        original = polyglot_build.ZIP64_SIZE_THRESHOLD
        polyglot_build.ZIP64_SIZE_THRESHOLD = 1024
        try:
            outer = self._make_file('outer.bin', b'OUTER' * 256)   # 1280 > 1024
            payload = b'SECRET_RAR_CONTENT' * 120                   # > 1024
            rar = self._make_file('secret.rar', payload)
            out = os.path.join(self.tmpdir, 'poly_z64.bin')

            build_polyglot(outer, rar, out, method=COMP_STORED)
            verify_polyglot(out)

            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(zf.read(zf.namelist()[0]), payload)
        finally:
            polyglot_build.ZIP64_SIZE_THRESHOLD = original

    def test_zip64_deflate_roundtrip(self):
        # 3.2 变体: ZIP64 + Deflate, 守护 P1 (本地头 ZIP64 extra 在 Deflate
        # 模式下 compressed_size 传 0 不写入未知值)
        original = polyglot_build.ZIP64_SIZE_THRESHOLD
        polyglot_build.ZIP64_SIZE_THRESHOLD = 1024
        try:
            outer = self._make_file('outer.bin', b'OUTER' * 256)
            payload = b'SECRET_RAR_CONTENT' * 200   # 高度可压缩
            rar = self._make_file('secret.rar', payload)
            out = os.path.join(self.tmpdir, 'poly_z64_def.bin')

            build_polyglot(outer, rar, out, method=COMP_DEFLATE)
            verify_polyglot(out)

            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(zf.read(zf.namelist()[0]), payload)
                info = zf.infolist()[0]
                self.assertLess(info.compress_size, info.file_size)
        finally:
            polyglot_build.ZIP64_SIZE_THRESHOLD = original


class TestVerify(unittest.TestCase):
    """守护 3.4: verify_polyglot 正/负用例。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='polyglot_verify_')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name, data):
        p = os.path.join(self.tmpdir, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def _build_valid(self):
        outer = self._make_file('outer.bin', b'OUTER_HEADER' + b'\x00' * 1024)
        payload = b'SECRET_RAR_CONTENT' * 100
        rar = self._make_file('secret.rar', payload)
        out = os.path.join(self.tmpdir, 'poly.bin')
        build_polyglot(outer, rar, out, method=COMP_STORED)
        return out, payload

    def test_verify_passes_on_valid_polyglot(self):
        out, _ = self._build_valid()
        self.assertTrue(verify_polyglot(out))

    def test_verify_emits_progress_callback(self):
        # A3: verify 应通过 'verify' 相位发出分块进度
        out, _ = self._build_valid()
        phases = []

        def cb(phase, cur, total, msg):
            phases.append((phase, cur, total))

        verify_polyglot(out, callback=cb)
        phase_names = [p[0] for p in phases]
        self.assertIn('info', phase_names)
        verify_p = [p for p in phases if p[0] == 'verify']
        self.assertGreater(len(verify_p), 0)
        # 最后一个 verify 进度应达到 total
        last = verify_p[-1]
        self.assertEqual(last[1], last[2])

    def test_verify_raises_on_corrupted_data(self):
        out, _ = self._build_valid()
        # 篡改存储数据区中的一个字节 (Store 模式下直接改变 CRC)
        # 数据起始 = 外层大小 + 本地头(30) + 文件名长度
        with open(out, 'r+b') as f:
            data = f.read()
            # 定位本地头签名后的数据区, 取一个肯定在 RAR 数据内的偏移
            corrupt_offset = data.find(b'secret.rar') + len('secret.rar') + 10
            f.seek(corrupt_offset)
            orig = f.read(1)
            f.seek(corrupt_offset)
            f.write(bytes([orig[0] ^ 0xFF]) if orig else b'\xFF')
        with self.assertRaises(IOError):
            verify_polyglot(out)

    def test_verify_raises_on_non_zip(self):
        junk = self._make_file('junk.bin', b'NOT A ZIP FILE ' * 50)
        with self.assertRaises(IOError):
            verify_polyglot(junk)

    def test_verify_raises_on_empty_file(self):
        empty = self._make_file('empty.bin', b'')
        with self.assertRaises(IOError):
            verify_polyglot(empty)


class TestOutputSaveOptions(unittest.TestCase):
    def test_jpg_outer_places_jpeg_first(self):
        filetypes, defaultext = get_output_save_options('photo.jpg')
        self.assertIn('JPEG', filetypes[0][0])
        self.assertEqual(filetypes[0][1], '*.jpg *.jpeg')
        self.assertEqual(defaultext, '.jpg')
        self.assertEqual(filetypes[-1][1], '*.*')

    def test_pdf_outer(self):
        filetypes, defaultext = get_output_save_options('doc.pdf')
        self.assertIn('PDF', filetypes[0][0])
        self.assertEqual(defaultext, '.pdf')

    def test_mp3_outer(self):
        filetypes, defaultext = get_output_save_options('a.mp3')
        self.assertIn('MP3', filetypes[0][0])
        self.assertEqual(defaultext, '.mp3')

    def test_unknown_ext_falls_back_to_mp4(self):
        filetypes, defaultext = get_output_save_options('file.xyz')
        self.assertNotEqual(filetypes[0][1], '*.xyz')
        self.assertEqual(defaultext, '.mp4')

    def test_empty_outer_falls_back_to_mp4(self):
        _, defaultext = get_output_save_options('')
        self.assertEqual(defaultext, '.mp4')

    def test_extension_is_case_insensitive(self):
        _, defaultext = get_output_save_options('PHOTO.JPG')
        self.assertEqual(defaultext, '.jpg')


class TestShouldFollowOuter(unittest.TestCase):
    def test_no_outer_never_follows(self):
        self.assertFalse(_should_follow_outer('', True, True))
        self.assertFalse(_should_follow_outer('', False, False))

    def test_empty_entry_follows(self):
        self.assertTrue(_should_follow_outer('a.mp4', False, False))

    def test_auto_filled_entry_follows_again(self):
        # 修复目标: 上次自动填充的值应再次被新外层覆盖
        self.assertTrue(_should_follow_outer('b.png', True, True))

    def test_user_edited_does_not_follow(self):
        # 用户主动改过输出后, 外层变化不应覆盖
        self.assertFalse(_should_follow_outer('a.mp4', True, False))


class TestCancelBuild(unittest.TestCase):
    """守护 1.1: build_polyglot 支持通过 stop_event 取消, 并正确清理半成品。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def _cancel_after_first_chunk(self, outer, rar, out, stop_event):
        """启动构建并在第一个 copy/compress 进度回调后触发取消。"""
        progress_reached = threading.Event()

        def cb(phase, cur, total, msg):
            if phase in ('copy', 'compress') and cur > 0:
                progress_reached.set()

        def target():
            try:
                build_polyglot(outer, rar, out, callback=cb,
                               method=COMP_STORED, stop_event=stop_event)
            except BuildCancelled:
                pass  # 预期中的取消, 不必打印线程异常

        # 把 CHUNK_SIZE 调小, 让回调/取消有充足机会发生
        original_chunk = polyglot_build.CHUNK_SIZE
        polyglot_build.CHUNK_SIZE = 64
        try:
            t = threading.Thread(target=target)
            t.start()
            self.assertTrue(progress_reached.wait(timeout=3),
                            '构建未在预期时间内产生进度')
            stop_event.set()
            t.join(timeout=3)
            self.assertFalse(t.is_alive(), '构建线程未在取消后结束')
        finally:
            polyglot_build.CHUNK_SIZE = original_chunk

    def test_cancel_deletes_partial_output(self):
        outer = self._make_file('outer.bin', b'OUTER' * 2000)
        rar = self._make_file('data.rar', b'RAR' * 2000)
        out = os.path.join(self.tmpdir, 'output.bin')

        stop = threading.Event()
        self._cancel_after_first_chunk(outer, rar, out, stop)

        self.assertFalse(os.path.exists(out),
                         '取消后应删除非覆盖模式下的半成品输出')

    def test_cancel_overwrite_restores_outer(self):
        original = b'ORIGINAL' * 500
        outer = self._make_file('outer.bin', original)
        rar = self._make_file('data.rar', b'RAR' * 2000)

        stop = threading.Event()
        self._cancel_after_first_chunk(outer, rar, outer, stop)

        self.assertTrue(os.path.exists(outer))
        with open(outer, 'rb') as f:
            self.assertEqual(f.read(), original,
                             '覆盖模式下取消应恢复原始外层文件')

    def test_cancel_raises_build_cancelled(self):
        outer = self._make_file('outer.bin', b'OUTER' * 2000)
        rar = self._make_file('data.rar', b'RAR' * 2000)
        out = os.path.join(self.tmpdir, 'output.bin')

        stop = threading.Event()
        stop.set()  # 直接取消, 不进入 IO

        with self.assertRaises(BuildCancelled):
            build_polyglot(outer, rar, out, stop_event=stop)

        self.assertFalse(os.path.exists(out))


class TestCLI(unittest.TestCase):
    """守护 1.2: CLI 参数与覆盖/取消行为。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def _run_main(self, argv):
        """以给定 argv 运行 main(), 返回 (exit_code, stdout, stderr)。"""
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from unittest import mock
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                polyglot_build.main()
                exit_code = 0
            except SystemExit as e:
                exit_code = e.code if e.code is not None else 0
        return exit_code, buf_out.getvalue(), buf_err.getvalue()

    def test_cli_quiet_build_success(self):
        outer = self._make_file('outer.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')

        code, _out, err = self._run_main(
            ['polyglot_build.py', outer, rar, '-o', out, '-q'])

        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(verify_polyglot(out))

    def test_cli_force_overwrites_without_prompt(self):
        outer = self._make_file('outer.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')
        with open(out, 'wb') as f:
            f.write(b'PREEXISTING')

        from unittest import mock
        # 若 input 被调用即视为失败
        with mock.patch('builtins.input',
                        side_effect=AssertionError('input 不应被调用')):
            code, _out, err = self._run_main(
                ['polyglot_build.py', outer, rar, '-o', out, '--force'])

        self.assertEqual(code, 0, err)
        with open(out, 'rb') as f:
            self.assertNotEqual(f.read(), b'PREEXISTING',
                                '--force 应覆盖已有文件')

    def test_cli_prompt_aborts_on_no(self):
        outer = self._make_file('outer.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')
        with open(out, 'wb') as f:
            f.write(b'PREEXISTING')

        from unittest import mock
        with mock.patch('builtins.input', return_value='n'):
            code, _out, _err = self._run_main(
                ['polyglot_build.py', outer, rar, '-o', out])

        self.assertEqual(code, 0)  # 用户拒绝, sys.exit(0)
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), b'PREEXISTING',
                             '用户拒绝覆盖时应保留原文件')

    def test_cli_no_verify_flag_runs(self):
        outer = self._make_file('outer.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')

        code, _out, err = self._run_main(
            ['polyglot_build.py', outer, rar, '-o', out, '-q', '--no-verify'])

        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out))


class TestProgressCallback(unittest.TestCase):
    """守护 P0: 非 TTY 下 progress_callback 不打印 \r 进度条 (避免重定向乱码)。"""

    def _run(self, tty):
        import io
        from unittest import mock

        class FakeStdout(io.StringIO):
            def isatty(self):
                return tty

        buf = FakeStdout()
        with mock.patch('sys.stdout', buf):
            progress_callback('compress', 50, 100, '进度测试')
            progress_callback('done', 100, 100, '完成')
        return buf.getvalue()

    def test_non_tty_no_carriage_return(self):
        out = self._run(tty=False)
        # 非 TTY: 不打印进度条, 无 \r; 仅 info/done 正常输出
        self.assertNotIn('\r', out)
        self.assertIn('完成', out)
        self.assertNotIn('[', out)  # 无进度条方块

    def test_tty_emits_progress_bar(self):
        out = self._run(tty=True)
        # TTY: 打印进度条 (含 \r 与 [ 和 █ 块字符)
        self.assertIn('\r', out)
        self.assertIn('[', out)
        self.assertIn('█', out)


class TestLogging(unittest.TestCase):
    """守护 P2.2: 统一日志层 get_logger/setup_logging 幂等且可配置。"""

    def test_get_logger_returns_same_singleton(self):
        import logging
        self.assertIs(get_logger(), get_logger())
        self.assertIs(get_logger(),
                      logging.getLogger(polyglot_build.LOGGER_NAME))

    def test_setup_logging_is_idempotent(self):
        import logging
        setup_logging(logging.DEBUG)
        setup_logging(logging.INFO)
        logger = get_logger()
        # 多次 setup 不应重复挂 handler
        self.assertEqual(len(logger.handlers), 1)
        self.assertEqual(logger.level, logging.INFO)


class TestVideoCompression(unittest.TestCase):
    """守护视频压缩: 档位、无 ffmpeg 时抛错、视频扩展名判断。"""

    def test_quality_tiers_defined(self):
        # 3 档质量 (high/medium/low) 各有码率与分辨率
        self.assertEqual(set(VIDEO_QUALITY), {'high', 'medium', 'low'})
        for key, (bitrate, max_h, _label) in VIDEO_QUALITY.items():
            self.assertGreater(bitrate, 0)
            self.assertGreater(max_h, 0)

    def test_invalid_quality_raises(self):
        with self.assertRaises(ValueError):
            compress_video('x.mp4', 'y.mp4', quality='bogus')

    def test_compress_video_raises_when_ffmpeg_missing(self):
        # mock find_ffmpeg 返回 None, 应抛 OSError
        from unittest import mock
        with mock.patch('polyglot_build.find_ffmpeg', return_value=None):
            with self.assertRaises(OSError):
                compress_video('x.mp4', 'y.mp4')

    def test_is_video_ext(self):
        self.assertTrue(PolyglotGUI._is_video_ext('a.mp4'))
        self.assertTrue(PolyglotGUI._is_video_ext('MOVIE.MKV'))
        self.assertFalse(PolyglotGUI._is_video_ext('photo.jpg'))
        self.assertFalse(PolyglotGUI._is_video_ext('doc.pdf'))


if __name__ == '__main__':
    unittest.main()
