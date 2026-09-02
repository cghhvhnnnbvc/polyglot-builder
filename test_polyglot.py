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
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polyglot_build
from polyglot_build import (
    build_data_descriptor, build_polyglot, verify_polyglot,
    generate_zip64_extra, progress_callback,
    get_logger, setup_logging, compress_video, find_ffmpeg,
    download_ffmpeg, FFMPEG_MIRRORS,
    VIDEO_QUALITY,
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

    def test_cli_compress_invokes_compress_video(self):
        # --compress: 先 compress_video 压缩外层, 再用压缩产物拼接
        from unittest import mock
        outer = self._make_file('movie.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')

        with mock.patch('polyglot_build.compress_video') as cv, \
                mock.patch('polyglot_build.build_polyglot') as bp, \
                mock.patch('polyglot_build.verify_polyglot'):
            code, _o, err = self._run_main(
                ['polyglot_build.py', outer, rar, '-o', out, '-q',
                 '--compress', 'medium'])

        self.assertEqual(code, 0, err)
        cv.assert_called_once()
        # call_args[1] 为 kwargs (兼容 3.6/3.7, 不用 .kwargs)
        self.assertEqual(cv.call_args[1].get('quality'), 'medium')
        bp.assert_called_once()
        # call_args[0][0] 为第一个位置参 (effective_outer)
        used_outer = bp.call_args[0][0]
        self.assertIn('polyglot_compressed_', used_outer,
                      'build_polyglot 应使用压缩产物作为外层')

    def test_cli_gui_flag_dispatches_to_gui(self):
        # --gui: 分发到 polyglot_gui.launch_gui 并 sys.exit(0)
        from unittest import mock
        import types
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest('tkinter 不可用')
        fake = types.ModuleType('polyglot_gui')
        fake.launch_gui = mock.MagicMock()
        with mock.patch.dict('sys.modules', {'polyglot_gui': fake}):
            code, _o, err = self._run_main(['polyglot_build.py', '--gui'])
        self.assertEqual(code, 0, err)
        fake.launch_gui.assert_called_once()

    def test_cli_no_args_dispatches_to_gui(self):
        # 无参数 (双击 exe / 直接运行) → 自动进入 GUI 并 sys.exit(0)
        from unittest import mock
        import types
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest('tkinter 不可用')
        fake = types.ModuleType('polyglot_gui')
        fake.launch_gui = mock.MagicMock()
        with mock.patch.dict('sys.modules', {'polyglot_gui': fake}):
            code, _o, err = self._run_main(['polyglot_build.py'])
        self.assertEqual(code, 0, err)
        fake.launch_gui.assert_called_once()

    def test_cli_partial_args_still_errors(self):
        # 只给外层、缺 RAR: 仍按 CLI 校验报错 (parser.error → SystemExit 2), 不误入 GUI
        code, _o, _err = self._run_main(
            ['polyglot_build.py', 'only_outer.mp4'])
        self.assertEqual(code, 2)


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


class TestFFmpegDownload(unittest.TestCase):
    """守护 ffmpeg 按需下载逻辑。"""

    def test_mirrors_defined(self):
        # 至少 1 个镜像, 每个含 (名称, URL)
        self.assertGreaterEqual(len(FFMPEG_MIRRORS), 1)
        for name, url in FFMPEG_MIRRORS:
            self.assertTrue(name)
            self.assertTrue(url.startswith('http'))

    def test_invalid_mirror_index_raises(self):
        with self.assertRaises(ValueError):
            download_ffmpeg(mirror_index=len(FFMPEG_MIRRORS))

    def test_non_windows_raises(self):
        # 非 Windows 平台不自动下载
        from unittest import mock
        with mock.patch('polyglot_build.os.name', 'posix'):
            with self.assertRaises(OSError):
                download_ffmpeg()

    def test_download_error_propagates(self):
        # 模拟 urllib 下载抛 URLError -> 转 OSError
        from unittest import mock
        tmp = tempfile.mkdtemp(prefix='ffmpeg_dl_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch('polyglot_build.os.name', 'nt'), \
                mock.patch('urllib.request.urlopen',
                           side_effect=urllib.error.URLError('net down')):
            with self.assertRaises(OSError):
                download_ffmpeg(dest_dir=tmp)

    def test_download_extracts_and_locates_exe(self):
        """完整链路: 下载 (mock 网络) -> 解压 -> _local_ffmpeg 定位到 exe。

        用本地构造的假 zip 充当 urlopen 返回, 不触碰真实网络/系统环境。
        验证 find_ffmpeg 缺失检测 -> 下载 -> 解压 -> 本地定位 这条核心路径。
        """
        import io
        from unittest import mock

        # 构造一个含 ffmpeg.exe 的假 zip (模拟 gyan.dev / BtbN 的目录结构)
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, 'w') as zf:
            zf.writestr('ffmpeg-release/bin/ffmpeg.exe', b'FAKE_FFMPEG_BINARY')
        zip_bytes.seek(0)

        class _FakeResp:
            """模仿 urllib 响应: 支持 headers 与 read()。"""
            def __init__(self, data):
                self._data = data
                self.headers = {'Content-Length': str(len(data))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                if n == -1:
                    chunk, self._data = self._data, b''
                else:
                    chunk, self._data = self._data[:n], self._data[n:]
                return chunk

        tmp = tempfile.mkdtemp(prefix='ffmpeg_dl_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # 隔离 _APP_DIR, 避免真实环境中 _APP_DIR/ffmpeg 下有缓存干扰
        app_dir = tempfile.mkdtemp(prefix='ffmpeg_app_')
        self.addCleanup(shutil.rmtree, app_dir, ignore_errors=True)

        # 把下载目录指向临时目录, 且确保系统 PATH 也无 ffmpeg
        with mock.patch('polyglot_build.os.name', 'nt'), \
                mock.patch('polyglot_build.FFMPEG_LOCAL_DIR', tmp), \
                mock.patch('polyglot_build._APP_DIR', app_dir), \
                mock.patch('shutil.which', return_value=None), \
                mock.patch('urllib.request.urlopen',
                           return_value=_FakeResp(zip_bytes.getvalue())):
            # 下载前: 本地无缓存 + PATH 无 -> find_ffmpeg 返回 None
            self.assertIsNone(find_ffmpeg(),
                              '下载前 find_ffmpeg 应返回 None (模拟干净环境)')
            # 执行下载
            exe = download_ffmpeg(dest_dir=tmp)
            # 下载后: _local_ffmpeg 能定位到解压出的 exe
            self.assertTrue(exe.endswith('ffmpeg.exe'),
                            f'解压后未定位到 ffmpeg.exe: {exe}')
            self.assertTrue(os.path.isfile(exe),
                            '定位到的 ffmpeg.exe 应真实存在')
            self.assertEqual(find_ffmpeg(), exe,
                             '下载后 find_ffmpeg 应返回刚解压的 exe 路径')

    def test_download_reports_progress(self):
        """下载过程应回调 callback('download', cur, total) 上报进度。"""
        import io
        from unittest import mock

        # 构造合法 zip, 内部放一个较大假 exe, 使分块 read 触发多次回调
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, 'w') as zf:
            zf.writestr('ffmpeg-release/bin/ffmpeg.exe',
                        b'FAKE_FFMPEG_BINARY' * (40 * 1024))  # 约 640KB
        data = zip_bytes.getvalue()

        class _FakeResp:
            def __init__(self, data):
                self._data = data
                self.headers = {'Content-Length': str(len(data))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                if n == -1:
                    chunk, self._data = self._data, b''
                else:
                    chunk, self._data = self._data[:n], self._data[n:]
                return chunk

        tmp = tempfile.mkdtemp(prefix='ffmpeg_dl_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        calls = []
        with mock.patch('polyglot_build.os.name', 'nt'), \
                mock.patch('polyglot_build.FFMPEG_LOCAL_DIR', tmp), \
                mock.patch('urllib.request.urlopen',
                           return_value=_FakeResp(data)):
            download_ffmpeg(dest_dir=tmp,
                            callback=lambda phase, cur, total, msg:
                            calls.append((phase, cur, total)))
        # 应至少上报一次 download 进度 (分块读取触发)
        self.assertTrue(any(p == 'download' for p, _c, _t in calls),
                        '应至少上报一次 download 进度')


class TestGuiRunFfmpegMissing(unittest.TestCase):
    """守护 GUI 构建流程: ffmpeg 缺失时走'提示下载'分支 (不真弹窗)。

    模拟干净环境 (find_ffmpeg 返回 None + 勾选压缩), 验证 _run 在构建前
    检测到 ffmpeg 缺失并触发下载引导, 而非直接调用 compress_video 失败。
    """

    def _build_gui(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        root.update()
        return root, gui

    def test_run_prompts_download_when_ffmpeg_missing(self):
        from unittest import mock

        root, gui = self._build_gui()
        self.addCleanup(root.destroy)

        # 勾选压缩表面视频
        gui._compress_var.set(True)

        # 记录 _run 内所有 root.after 入队 (避免真弹窗/真构建, 仅验证分支)
        after_calls = []
        tmpdir = tempfile.mkdtemp(prefix='gui_run_')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        outer = os.path.join(tmpdir, 'movie.mp4')  # 扩展名必须是视频
        rar = os.path.join(tmpdir, 'data.rar')
        out = os.path.join(tmpdir, 'output.bin')
        with mock.patch('polyglot_gui.find_ffmpeg', return_value=None), \
                mock.patch.object(root, 'after',
                                  side_effect=lambda *a, **k: after_calls.append(a)), \
                mock.patch('polyglot_gui.build_polyglot') as build:
            gui._run(outer, rar, out)

        # 应触发下载引导: after 队列里包含 _prompt_download_ffmpeg 入队
        # 注意: 每次访问 gui._prompt_download_ffmpeg 生成新的绑定方法对象,
        # 故比较底层 __func__ (稳定身份) 而非 is 绑定方法
        prompt_queued = any(
            len(c) >= 2
            and getattr(c[1], '__func__', None) is PolyglotGUI._prompt_download_ffmpeg
            for c in after_calls
        )
        self.assertTrue(prompt_queued,
                        'ffmpeg 缺失时应将引导下载入队 (而非直接构建失败)')
        # 不应进入真正构建 (避免压缩失败)
        build.assert_not_called()


class TestGuiLayout(unittest.TestCase):
    """守护 GUI 布局: 防止列争抢宽度导致标题/卡片/按钮被挤压错乱。

    复现 2026-08-08 反馈: compress_frame 在 column=1 时撑爆窗口,
    把标题 (被裁切为 'Polyglc') / 文件选择卡片 (被压成窄列) /
    按钮 (被'取消'覆盖) 全挤压错乱。修复后 compress_frame 合并到
    opt_frame (column=0), 全部用 pack 横向排布, 不再争列宽度。
    """

    def _build_gui(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        root.update()  # 强制传播几何信息
        return root, gui

    def _iter_descendants(self, widget):
        """深度优先迭代 widget 的所有后代控件。"""
        stack = list(widget.winfo_children())
        while stack:
            w = stack.pop()
            yield w
            stack.extend(w.winfo_children())

    def test_root_window_normal_width(self):
        # 根窗口宽度应在 880 附近 (之前被压缩列撑爆后变小或被错误几何拉伸)
        root, _ = self._build_gui()
        self.assertGreater(root.winfo_width(), 800)

    def test_no_child_overflows_root_width(self):
        # 任何子控件宽度不应超过根窗口宽度 (防止某列撑爆)
        root, _ = self._build_gui()
        root_w = root.winfo_width()
        for w in self._iter_descendants(root):
            cw = w.winfo_width()
            # 允许 1px 误差
            self.assertLessEqual(cw, root_w,
                                 f'控件 {w} 宽度 {cw} 超过根窗口 {root_w}')

    def test_card_frame_wide_enough(self):
        # 文件选择卡片宽度应 > 根窗口的 80% (之前被压到约 60px)
        root, _ = self._build_gui()
        root_w = root.winfo_width()
        card = None
        for w in self._iter_descendants(root):
            try:
                bg = str(w.cget('bg'))
            except Exception:
                continue
            # 文件卡片背景为 C_CARD (#FFFFFF)
            if bg == '#FFFFFF':
                card = w
                break
        self.assertIsNotNone(card, '未找到文件选择卡片')
        self.assertGreater(card.winfo_width(), int(root_w * 0.8),
                           f'文件卡片宽度 {card.winfo_width()} 过窄 (窗口 {root_w})')

    def test_title_not_truncated(self):
        # 标题 'Polyglot Builder' 的宽度应 > 100px (之前被裁切为 'Polyglc')
        root, _ = self._build_gui()
        title = None
        for w in self._iter_descendants(root):
            try:
                if w.cget('text') == 'Polyglot Builder':
                    title = w
                    break
            except Exception:
                continue
        self.assertIsNotNone(title, '未找到标题')
        self.assertGreater(title.winfo_width(), 100,
                           f'标题被裁切: 宽度仅 {title.winfo_width()}')


class TestVersionConsistency(unittest.TestCase):
    """守护 C3: VERSION 常量与 bat 启动器中硬编码版本号一致, 防漂移。"""

    def test_version_format(self):
        import re
        self.assertRegex(polyglot_build.VERSION, r'^\d+\.\d+$')

    def test_bat_versions_match_constant(self):
        import re
        ver = polyglot_build.VERSION
        base = os.path.dirname(os.path.abspath(__file__))
        checked = 0
        for bat in ('polyglot_build.bat', '启动GUI.bat'):
            path = os.path.join(base, bat)
            if not os.path.isfile(path):
                continue
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            for m in re.findall(r'v(\d+\.\d+)', text):
                self.assertEqual(
                    m, ver,
                    f'{bat} 中 v{m} 与 polyglot_build.VERSION={ver} 不一致')
                checked += 1
        self.assertGreater(checked, 0,
                           '未在任何 bat 中找到版本号, 一致性守护失效')


class TestCompressVideoProgress(unittest.TestCase):
    """守护 1.4: compress_video 解析 ffmpeg -progress 输出并换算百分比进度。"""

    def _fake_proc(self, lines, returncode=0):
        class _FakeProc:
            def __init__(self):
                self.stdout = iter(lines)
                self.returncode = returncode

            def wait(self, *a, **k):
                return self.returncode

            def poll(self):
                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                pass
        return _FakeProc()

    def test_progress_emits_increasing_pct(self):
        from unittest import mock
        proc = self._fake_proc([
            b'frame=10\n',
            b'out_time=00:00:02.000000\n',
            b'progress=continue\n',
            b'out_time=00:00:05.000000\n',
            b'out_time=00:00:09.000000\n',
            b'progress=end\n',
        ])
        pcts = []

        def cb(phase, cur, total, msg):
            if phase == 'compress' and total == 100:
                pcts.append(cur)

        with mock.patch('polyglot_build.find_ffmpeg', return_value='/bin/ffmpeg'), \
                mock.patch('polyglot_build._find_ffprobe', return_value='/bin/ffprobe'), \
                mock.patch('polyglot_build._probe_duration', return_value=10.0), \
                mock.patch('polyglot_build.subprocess.Popen', return_value=proc):
            compress_video('in.mp4', 'out.mp4', quality='medium', callback=cb)

        # out_time 2/5/9 秒, duration 10 秒 -> 20% / 50% / 90%
        self.assertEqual(pcts, [20, 50, 90])

    def test_no_progress_when_duration_unknown(self):
        # ffprobe 缺失 (duration=None): 仍排空 stdout 不报错, 但不产出百分比进度
        from unittest import mock
        proc = self._fake_proc([b'out_time=00:00:03.000000\n', b'progress=end\n'])
        phases = []

        def cb(phase, cur, total, msg):
            phases.append(phase)

        with mock.patch('polyglot_build.find_ffmpeg', return_value='/bin/ffmpeg'), \
                mock.patch('polyglot_build._find_ffprobe', return_value=None), \
                mock.patch('polyglot_build.subprocess.Popen', return_value=proc):
            compress_video('in.mp4', 'out.mp4', quality='low', callback=cb)

        self.assertNotIn('compress', phases)
        self.assertIn('info', phases)


class TestDosDatetime(unittest.TestCase):
    """守护 4.1: _dos_datetime_from_mtime 的 DOS 时间/日期换算。"""

    def test_known_local_datetime_roundtrip(self):
        # 构造确定的本地时间 (2021-03-04 05:06:08); mktime/localtime 同时区, 可往返
        mtime = time.mktime((2021, 3, 4, 5, 6, 8, 0, 0, -1))
        dos_time, dos_date = polyglot_build._dos_datetime_from_mtime(mtime)
        # DOS 时间: 时<<11 | 分<<5 | 秒//2
        self.assertEqual(dos_time >> 11, 5)
        self.assertEqual((dos_time >> 5) & 0x3F, 6)
        self.assertEqual(dos_time & 0x1F, 4)   # 8 // 2
        # DOS 日期: (年-1980)<<9 | 月<<5 | 日
        self.assertEqual((dos_date >> 9) + 1980, 2021)
        self.assertEqual((dos_date >> 5) & 0x0F, 3)
        self.assertEqual(dos_date & 0x1F, 4)

    def test_pre_1980_clamped_to_dos_epoch(self):
        # epoch 0 (1970/1969 视时区) 早于 DOS 纪元, 年份应钳制到 1980
        _dos_time, dos_date = polyglot_build._dos_datetime_from_mtime(0)
        self.assertEqual((dos_date >> 9) + 1980, 1980)


class TestValidateRar(unittest.TestCase):
    """守护 4.3: _validate_rar 魔数校验 (是 RAR 返回 None, 否则返回警告)。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, data):
        p = os.path.join(self.tmpdir, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def test_rar5_magic_ok(self):
        p = self._write('a.rar', polyglot_build.RAR5_MAGIC + b'\x00' * 20)
        self.assertIsNone(polyglot_build._validate_rar(p))

    def test_rar4_magic_ok(self):
        p = self._write('b.rar', polyglot_build.RAR4_MAGIC + b'\x00' * 20)
        self.assertIsNone(polyglot_build._validate_rar(p))

    def test_non_rar_returns_warning(self):
        p = self._write('c.bin', b'NOT A RAR FILE AT ALL')
        warn = polyglot_build._validate_rar(p)
        self.assertIsNotNone(warn)
        self.assertIn('RAR', warn)

    def test_missing_file_returns_warning(self):
        warn = polyglot_build._validate_rar(
            os.path.join(self.tmpdir, 'ghost.rar'))
        self.assertIsNotNone(warn)
        self.assertIn('无法读取', warn)


class TestLogFilePersistence(unittest.TestCase):
    """守护 4.4: setup_logging(log_file=...) 挂 FileHandler 且幂等、可写。"""

    def setUp(self):
        import logging
        self.tmpdir = tempfile.mkdtemp()
        self._logging = logging

    def tearDown(self):
        # 清理本类挂上的 FileHandler, 避免污染模块级 logger 单例
        logger = get_logger()
        for h in list(logger.handlers):
            if isinstance(h, self._logging.FileHandler):
                logger.removeHandler(h)
                h.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_log_file_handler_attached_and_written(self):
        path = os.path.join(self.tmpdir, 'run.log')
        setup_logging(self._logging.INFO, log_file=path)
        logger = get_logger()
        fhs = [h for h in logger.handlers
               if isinstance(h, self._logging.FileHandler)]
        self.assertEqual(len(fhs), 1)
        # 幂等: 同一文件再次调用不重复挂
        setup_logging(self._logging.INFO, log_file=path)
        fhs = [h for h in logger.handlers
               if isinstance(h, self._logging.FileHandler)]
        self.assertEqual(len(fhs), 1)
        # 写入日志 -> 文件内容包含该行 (文件 handler 带级别前缀)
        logger.info('persisted-line-xyz')
        for h in fhs:
            h.flush()
        with open(path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('persisted-line-xyz', content)
        self.assertIn('INFO', content)


class TestBatchMode(unittest.TestCase):
    """守护 4.5: _parse_batch_manifest 解析 + --batch 端到端汇总成败。"""

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

    def test_parse_manifest_basic(self):
        manifest = os.path.join(self.tmpdir, 'm.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write('# 注释行\n')
            f.write('\n')
            f.write('  a.mp4 | a.rar \n')       # 2 段: output 默认= outer
            f.write('b.mp4|b.rar|out_b.mp4\n')  # 3 段: 显式 output
        tasks = polyglot_build._parse_batch_manifest(manifest)
        self.assertEqual(tasks, [
            ('a.mp4', 'a.rar', 'a.mp4'),
            ('b.mp4', 'b.rar', 'out_b.mp4'),
        ])

    def test_parse_manifest_invalid_line_raises(self):
        manifest = os.path.join(self.tmpdir, 'bad.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write('only_one_field\n')
        with self.assertRaises(ValueError):
            polyglot_build._parse_batch_manifest(manifest)

    def test_parse_manifest_missing_file_raises(self):
        with self.assertRaises(IOError):
            polyglot_build._parse_batch_manifest(
                os.path.join(self.tmpdir, 'nope.txt'))

    def test_batch_end_to_end_success(self):
        o1 = self._make_file('v1.bin', b'OUTER1' * 50)
        r1 = self._make_file('d1.rar', polyglot_build.RAR5_MAGIC + b'X' * 50)
        out1 = os.path.join(self.tmpdir, 'out1.bin')
        o2 = self._make_file('v2.bin', b'OUTER2' * 50)
        r2 = self._make_file('d2.rar', polyglot_build.RAR4_MAGIC + b'Y' * 50)
        out2 = os.path.join(self.tmpdir, 'out2.bin')
        manifest = os.path.join(self.tmpdir, 'batch.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write(f'{o1}|{r1}|{out1}\n')
            f.write(f'{o2}|{r2}|{out2}\n')
        code, _o, err = self._run_main(
            ['polyglot_build.py', '--batch', manifest, '-q'])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out1))
        self.assertTrue(os.path.exists(out2))
        self.assertTrue(verify_polyglot(out1))
        self.assertTrue(verify_polyglot(out2))

    def test_batch_partial_failure_exits_1(self):
        o1 = self._make_file('ok.bin', b'OUTER' * 50)
        r1 = self._make_file('ok.rar', polyglot_build.RAR5_MAGIC + b'Z' * 50)
        out1 = os.path.join(self.tmpdir, 'ok_out.bin')
        missing = os.path.join(self.tmpdir, 'missing.bin')
        m_out = os.path.join(self.tmpdir, 'm_out.bin')
        manifest = os.path.join(self.tmpdir, 'mixed.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write(f'{o1}|{r1}|{out1}\n')
            f.write(f'{missing}|{r1}|{m_out}\n')  # 外层不存在 -> 失败不中断
        code, _o, err = self._run_main(
            ['polyglot_build.py', '--batch', manifest, '-q'])
        self.assertEqual(code, 1, err)
        # 第一条仍应成功产出
        self.assertTrue(os.path.exists(out1))


if __name__ == '__main__':
    unittest.main()
