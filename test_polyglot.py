# -*- coding: utf-8 -*-
"""Polyglot Builder 回归测试 (标准库 unittest, 零依赖)。

守护 P0 修复:
  1. build_data_descriptor 的 ZIP64 分支字段顺序必须为
     signature -> CRC-32 -> compressed -> uncompressed (<IIQQ)。
  2. 端到端构建 (Store 模式) 生成的文件能被 zipfile 校验通过且内容还原。
  3. 输出覆盖外层文件时, 临时副本在构建完成后被清理。
"""

import os
import json
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
from unittest import mock
from dataclasses import asdict

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
from polyglot_gui import (get_output_save_options, _should_follow_outer,
                          PolyglotGUI, LedgerRecordDialog, LedgerManagerDialog,
                          messagebox)
import polyglot_gui
import polyglot_ledger
from polyglot_ledger import (LedgerError, LedgerRecord, append_record,
                             create_ledger, load_records, open_ledger,
                             save_records)


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

    def test_cli_defaults_to_cpu_encoder(self):
        """不加 --hw-encoder 时应走 CPU 编码 (use_hw=False)。"""
        from unittest import mock
        outer = self._make_file('m2.bin', b'OUTER' * 50)
        rar = self._make_file('d2.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'o2.bin')
        with mock.patch('polyglot_build.compress_video') as cv, \
                mock.patch('polyglot_build.build_polyglot'), \
                mock.patch('polyglot_build.verify_polyglot'):
            code, _o, err = self._run_main(
                ['polyglot_build.py', outer, rar, '-o', out, '-q',
                 '--compress', 'medium'])
        self.assertEqual(code, 0, err)
        self.assertIs(cv.call_args[1].get('use_hw'), False)

    def test_cli_hw_encoder_flag_passed_through(self):
        """--hw-encoder 应透传给 compress_video (单文件与批量模式)。"""
        from unittest import mock
        outer = self._make_file('m3.bin', b'OUTER' * 50)
        rar = self._make_file('d3.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'o3.bin')
        with mock.patch('polyglot_build.compress_video') as cv, \
                mock.patch('polyglot_build.build_polyglot'), \
                mock.patch('polyglot_build.verify_polyglot'):
            code, _o, err = self._run_main(
                ['polyglot_build.py', outer, rar, '-o', out, '-q',
                 '--compress', 'low', '--hw-encoder'])
        self.assertEqual(code, 0, err)
        self.assertIs(cv.call_args[1].get('use_hw'), True)
        self.assertEqual(cv.call_args[1].get('quality'), 'low')

    def test_cli_batch_passes_hw_encoder(self):
        from unittest import mock
        outer = self._make_file('b1.bin', b'OUTER' * 50)
        rar = self._make_file('b1.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'b1out.bin')
        manifest = os.path.join(self.tmpdir, 'b.txt')
        with open(manifest, 'w', encoding='utf-8') as f:
            f.write(f'{outer}|{rar}|{out}\n')
        with mock.patch('polyglot_build.compress_video') as cv, \
                mock.patch('polyglot_build.build_polyglot'), \
                mock.patch('polyglot_build.verify_polyglot'):
            code, _o, err = self._run_main(
                ['polyglot_build.py', '--batch', manifest, '-q',
                 '--compress', 'medium', '--hw-encoder'])
        self.assertEqual(code, 0, err)
        cv.assert_called_once()
        self.assertIs(cv.call_args[1].get('use_hw'), True)

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


class TestGuiHwEncoderToggle(unittest.TestCase):
    """守护 GUI 硬件编码开关: 默认关闭/禁用, 随压缩勾选启用, 且透传给 compress_video。"""

    def _build_gui(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        return root, gui

    def test_hw_checkbox_default_off_and_disabled(self):
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        self.assertIs(gui._hw_var.get(), False, '硬件编码应默认关闭 (实测 CPU 更快)')
        self.assertEqual(str(gui._hw_cb['state']), 'disabled',
                         '未勾选压缩时开关应置灰')

    def test_hw_checkbox_follows_compress_toggle(self):
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        gui._compress_var.set(True)
        gui._on_compress_toggle()
        self.assertEqual(str(gui._hw_cb['state']), 'normal')
        gui._compress_var.set(False)
        gui._on_compress_toggle()
        self.assertEqual(str(gui._hw_cb['state']), 'disabled')

    def _run_with(self, gui, root, hw):
        from unittest import mock
        tmpdir = tempfile.mkdtemp(prefix='gui_hw_')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        outer = os.path.join(tmpdir, 'movie.mp4')
        rar = os.path.join(tmpdir, 'data.rar')
        out = os.path.join(tmpdir, 'output.bin')
        gui._compress_var.set(True)
        gui._hw_var.set(hw)
        gui._on_compress_toggle()
        with mock.patch('polyglot_gui.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_gui.compress_video') as cv, \
                mock.patch('polyglot_gui.build_polyglot'), \
                mock.patch('polyglot_gui.verify_polyglot'), \
                mock.patch.object(root, 'after', lambda *a, **k: None):
            gui._run(outer, rar, out)
        return cv

    def test_run_passes_use_hw_true_when_checked(self):
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        cv = self._run_with(gui, root, True)
        cv.assert_called_once()
        self.assertIs(cv.call_args[1].get('use_hw'), True)

    def test_run_passes_use_hw_false_by_default(self):
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        cv = self._run_with(gui, root, False)
        cv.assert_called_once()
        self.assertIs(cv.call_args[1].get('use_hw'), False)

    def test_compression_controls_grouped_in_one_row(self):
        """压缩表面视频的开关/档位/硬件编码必须同排, 且与 Deflate 分行。

        v1.2.1 的 2x2 布局把档位+硬件编码与开关拆到两行, 读起来像
        另一组独立功能; 现改为整组同排 + 左缩进表达从属。
        """
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        row2 = gui._quality_combo.master
        self.assertIs(gui._hw_cb.master, row2,
                      '硬件编码应与档位下拉同排')
        kids = row2.winfo_children()
        compress_in_row2 = any(
            c.winfo_class() == 'Checkbutton'
            and str(c.cget('variable')) == str(gui._compress_var)
            for c in kids)
        self.assertTrue(compress_in_row2,
                        '压缩表面视频开关应与档位/硬件编码同排')
        # Deflate 在另一行 (它是内层 RAR 压缩, 与表面视频压缩是两回事)
        row1 = gui._quality_combo.master.master.winfo_children()[0]
        self.assertIsNot(row1, row2, 'Deflate 与压缩表面视频应分行')
        deflate_in_row1 = any(
            c.winfo_class() == 'Checkbutton'
            and str(c.cget('variable')) == str(gui._deflate_var)
            for c in row1.winfo_children())
        self.assertTrue(deflate_in_row1, 'Deflate 应单独一行')

    def test_selected_quality_reverse_mapping(self):
        """档位下拉显示短文案, key 靠反向映射解析 (不再用前缀匹配)。"""
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        gui._compress_var.set(True)
        for key in ('high', 'medium', 'low'):
            gui._quality_combo.set(polyglot_build.VIDEO_QUALITY[key][2])
            self.assertEqual(gui._selected_quality(), key)
        # 未勾选压缩时返回 None (不压缩)
        gui._compress_var.set(False)
        self.assertIsNone(gui._selected_quality())

    def test_quality_combo_default_shows_medium_label(self):
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        self.assertEqual(
            gui._quality_combo.get(),
            polyglot_build.VIDEO_QUALITY[polyglot_build.DEFAULT_VIDEO_QUALITY][2])

    def test_run_skips_compression_when_compress_unchecked(self):
        """复选框才是压缩开关: 未勾选时不应调用 compress_video。"""
        from unittest import mock
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        tmpdir = tempfile.mkdtemp(prefix='gui_nocomp_')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        outer = os.path.join(tmpdir, 'movie.mp4')
        rar = os.path.join(tmpdir, 'data.rar')
        out = os.path.join(tmpdir, 'output.bin')
        gui._compress_var.set(False)
        with mock.patch('polyglot_gui.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_gui.compress_video') as cv, \
                mock.patch('polyglot_gui.build_polyglot') as bp, \
                mock.patch('polyglot_gui.verify_polyglot'), \
                mock.patch.object(root, 'after', lambda *a, **k: None):
            gui._run(outer, rar, out)
        cv.assert_not_called()
        bp.assert_called_once()
        self.assertEqual(bp.call_args[0][0], outer,
                         '未勾选压缩时外层应原样使用, 不应走压缩产物')

    def test_option_rows_fit_at_large_font(self):
        """模拟 150% 缩放: 选项区每一行都不应超宽 (防裁切回归)。"""
        import tkinter as tk
        saved = polyglot_gui.FONT_ENTRY
        polyglot_gui.FONT_ENTRY = (saved[0], 15)
        self.addCleanup(setattr, polyglot_gui, 'FONT_ENTRY', saved)
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境: {e}')
        self.addCleanup(root.destroy)
        root.geometry('880x700')
        with mock.patch.object(polyglot_gui, '_resolve_fonts', lambda r: None):
            gui = PolyglotGUI(root)
        root.update_idletasks()
        root.update()
        opt = gui._quality_combo.master.master
        for i, row in enumerate(opt.winfo_children(), 1):
            req = sum(c.winfo_reqwidth() for c in row.winfo_children())
            avail = row.winfo_width()
            self.assertLessEqual(
                req, avail,
                f'150% 缩放下选项区第 {i} 行超宽 ({req} > {avail}), 会裁切控件')

    def test_hw_checkbox_visible_when_first_row_overflows(self):
        """模拟大字体环境: 第一行被撑宽时, 硬件编码仍应完整可见。"""
        import tkinter as tk
        saved = polyglot_gui.FONT_ENTRY
        polyglot_gui.FONT_ENTRY = (saved[0], 20)   # 放大下拉等控件, 模拟高 DPI
        self.addCleanup(setattr, polyglot_gui, 'FONT_ENTRY', saved)
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境: {e}')
        self.addCleanup(root.destroy)
        root.geometry('880x700')
        # 跳过字体自适应, 让放大后的 FONT_ENTRY 生效
        with mock.patch.object(polyglot_gui, '_resolve_fonts', lambda r: None):
            gui = PolyglotGUI(root)
        root.update_idletasks()
        root.update()
        win_right = root.winfo_rootx() + root.winfo_width()
        right = gui._hw_cb.winfo_rootx() + gui._hw_cb.winfo_width()
        self.assertLessEqual(
            right, win_right,
            f'大字体下硬件编码勾选被裁在窗口外 (右缘 {right} > 窗口 {win_right})')


class TestGuiLedgerRestore(unittest.TestCase):
    """守护台账恢复路径: 选到已存在的台账 .json 时绝不可清空覆盖。"""

    def _build_gui(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        return root, gui

    def test_open_ledger_never_overwrites_existing_file(self):
        from unittest import mock
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        tmpdir = tempfile.mkdtemp(prefix='ledger_restore_')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        existing = os.path.join(tmpdir, '资源台账.json')
        polyglot_ledger.append_record(
            existing, polyglot_ledger.LedgerRecord(name='备份里的记录'))
        with mock.patch('polyglot_gui.resolve_ledger_path',
                        return_value=os.path.join(tmpdir, 'missing.json')), \
                mock.patch.object(messagebox, 'askyesno', return_value=True), \
                mock.patch.object(polyglot_gui.filedialog, 'asksaveasfilename',
                                  return_value=existing), \
                mock.patch('polyglot_gui.save_configured_path'), \
                mock.patch('polyglot_gui.LedgerManagerDialog'):
            gui._open_ledger()
        recs = polyglot_ledger.load_records(existing)
        self.assertEqual([r.name for r in recs], ['备份里的记录'],
                         '选到已存在的台账时必须直接使用, 绝不可清空覆盖')

    def test_open_ledger_creates_when_path_is_new(self):
        from unittest import mock
        root, gui = self._build_gui()
        self.addCleanup(root.destroy)
        tmpdir = tempfile.mkdtemp(prefix='ledger_new_')
        self.addCleanup(shutil.rmtree, tmpdir, True)
        target = os.path.join(tmpdir, '新台账.json')
        with mock.patch('polyglot_gui.resolve_ledger_path',
                        return_value=os.path.join(tmpdir, 'missing.json')), \
                mock.patch.object(messagebox, 'askyesno', return_value=True), \
                mock.patch.object(polyglot_gui.filedialog, 'asksaveasfilename',
                                  return_value=target), \
                mock.patch('polyglot_gui.save_configured_path'), \
                mock.patch('polyglot_gui.LedgerManagerDialog'):
            gui._open_ledger()
        self.assertTrue(os.path.isfile(target), '新位置应创建空台账')
        self.assertEqual(polyglot_ledger.load_records(target), [])


class TestGuiPollingCleanup(unittest.TestCase):
    """守护关窗/销毁时取消日志轮询 (避免 Tk 报 invalid command name)。"""

    def _build_gui(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        return root, gui

    def test_poll_after_id_tracked_and_cancellable(self):
        root, gui = self._build_gui()
        self.addCleanup(gui._stop_polling)
        self.addCleanup(root.destroy)
        self.assertIsNotNone(gui._poll_after_id, '初始化后应有一个待执行的轮询回调')
        gui._stop_polling()
        self.assertIsNone(gui._poll_after_id)
        gui._stop_polling()   # 重复调用不应报错

    def test_on_close_stops_build_and_destroys_window(self):
        import tkinter as tk
        root, gui = self._build_gui()
        gui._on_close()
        self.assertTrue(gui._stop_event.is_set(), '关窗应中止进行中的构建')
        self.assertIsNone(gui._poll_after_id)
        # 根窗口已销毁: 再向其发 Tcl 命令应报错
        with self.assertRaises(tk.TclError):
            root.winfo_exists()


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
        # 允许两段 (1.2) 或三段 (1.2.1) 语义化版本
        self.assertRegex(polyglot_build.VERSION, r'^\d+\.\d+(\.\d+)?$')

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
            for m in re.findall(r'v(\d+\.\d+(?:\.\d+)?)', text):
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


class TestLedger(unittest.TestCase):
    """守护资源台账 (JSON 数据源 + 自动生成 HTML 查看页) 的读写与容错。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, '资源台账.json')
        self.view = polyglot_ledger.html_view_path(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_legacy_html(self, html_path, payload='[]'):
        """造一个旧版单文件 HTML 台账 (仅含数据块, 用于迁移测试)。"""
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html><html><body>旧版台账'
                    '<script id="ledger-data" type="application/json">'
                    + payload +
                    '</script></body></html>')

    def test_create_makes_json_and_html_view(self):
        create_ledger(self.path)
        self.assertTrue(os.path.isfile(self.path), '应生成 JSON 数据文件')
        self.assertTrue(os.path.isfile(self.view), '应同时生成 HTML 查看页')
        self.assertEqual(load_records(self.path), [])

    def test_json_is_source_of_truth(self):
        """手改 JSON 应被读到, 且保存时 HTML 查看页跟着重生。"""
        create_ledger(self.path)
        with open(self.view, 'w', encoding='utf-8') as f:
            f.write('<html>现查看页已过期</html>')
        rec = LedgerRecord(name='手改JSON', filename='x.mp4')
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'records': [asdict(rec)]},
                      f, ensure_ascii=False)
        got = load_records(self.path)
        self.assertEqual([r.name for r in got], ['手改JSON'])
        # 重新保存 -> 查看页由 JSON 重建, 过期内容被覆盖
        save_records(self.path, got)
        with open(self.view, encoding='utf-8') as f:
            self.assertNotIn('已过期', f.read())

    def test_html_view_deletable_and_regenerated(self):
        """查看页是衍生物: 删了也不影响数据, 打开时自动重建。"""
        append_record(self.path, LedgerRecord(name='保留'))
        os.remove(self.view)
        self.assertEqual(load_records(self.path)[0].name, '保留')
        with mock.patch.object(polyglot_ledger.webbrowser, 'open') as browser:
            open_ledger(self.path)
        self.assertTrue(os.path.isfile(self.view), '查看页应被重建')
        browser.assert_called_once()

    def test_append_roundtrip_unicode(self):
        rec = LedgerRecord(name='某游戏整合包', filename='game.mp4', size='1.5 GB',
                           date='2026-09-03 10:00', netdisk='百度网盘',
                           netdisk_path='/我的资源/2026', share_link='https://x/y',
                           share_code='ab12', rar_password='密码P@ss',
                           note='含中文与符号 "\'<>')
        append_record(self.path, rec)
        got = load_records(self.path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], rec)

    def test_append_twice_keeps_order(self):
        append_record(self.path, LedgerRecord(name='A', filename='a.mp4'))
        append_record(self.path, LedgerRecord(name='B', filename='b.mp4'))
        got = load_records(self.path)
        self.assertEqual([r.name for r in got], ['A', 'B'])

    def test_script_close_tag_escaped_in_view_only(self):
        """值里的 </script> 不得提前闭合查看页数据块; JSON 数据源保持原样。"""
        append_record(self.path, LedgerRecord(name='x</script><b>y', note='</script>'))
        with open(self.view, encoding='utf-8') as f:
            html = f.read()
        self.assertEqual(html.count('<script id="ledger-data"'), 1)
        with open(self.path, encoding='utf-8') as f:
            raw = f.read()
        self.assertIn('x</script><b>y', raw, 'JSON 数据源不应被转义污染')
        got = load_records(self.path)
        self.assertEqual(got[0].name, 'x</script><b>y')
        self.assertEqual(got[0].note, '</script>')

    def test_append_creates_missing_ledger(self):
        self.assertFalse(os.path.isfile(self.path))
        append_record(self.path, LedgerRecord(name='auto'))
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(load_records(self.path)[0].name, 'auto')

    def test_no_tmp_file_left_behind(self):
        """原子写入不应残留 .tmp 文件。"""
        append_record(self.path, LedgerRecord(name='A'))
        leftovers = [n for n in os.listdir(self.tmpdir) if n.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_load_missing_ledger_raises(self):
        with self.assertRaises(LedgerError):
            load_records(os.path.join(self.tmpdir, 'nope.json'))

    def test_load_corrupt_json_raises(self):
        create_ledger(self.path)
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{坏数据')
        with self.assertRaises(LedgerError):
            load_records(self.path)

    def test_legacy_html_migration(self):
        """旧版单文件 HTML 台账: 自动转 JSON + 旧文件改名 .bak + 生成新查看页。"""
        legacy = json.dumps([{'name': '旧资源', 'filename': 'old.mp4',
                              'size': '3 MB', 'date': '2026-08-01 09:00',
                              'netdisk': '夸克网盘', 'netdisk_path': '/旧',
                              'share_link': '', 'share_code': '',
                              'rar_password': 'oldpw', 'note': ''}],
                            ensure_ascii=False)
        self._write_legacy_html(self.view, legacy)
        self.assertFalse(os.path.isfile(self.path))

        got = load_records(self.path)
        self.assertEqual([r.name for r in got], ['旧资源'])
        self.assertEqual(got[0].rar_password, 'oldpw')
        self.assertTrue(os.path.isfile(self.path), '应生成 JSON 数据文件')
        self.assertTrue(os.path.isfile(self.view + '.bak'), '旧 HTML 应保留为 .bak')
        with open(self.view, encoding='utf-8') as f:
            self.assertIn('旧资源', f.read(), '应生成新查看页')

    def test_legacy_html_path_argument_migrates(self):
        """传入旧版 .html 路径也能用 (规范化 + 迁移)。"""
        self._write_legacy_html(
            self.view, json.dumps([{'name': '从 HTML 路径读'}], ensure_ascii=False))
        got = load_records(self.view)
        self.assertEqual(got[0].name, '从 HTML 路径读')
        self.assertTrue(os.path.isfile(self.path))

    def test_legacy_html_without_data_block_raises(self):
        self._write_legacy_html(self.view, '')
        with open(self.view, 'w', encoding='utf-8') as f:
            f.write('<html><body>不是台账</body></html>')
        with self.assertRaises(LedgerError):
            load_records(self.path)

    def test_html_view_contains_viewer_features(self):
        create_ledger(self.path)
        with open(self.view, encoding='utf-8') as f:
            html = f.read()
        for needle in ('charset="utf-8"', 'ledger-data', '导出 CSV',
                       '全部网盘', 'RAR 密码', '明文存储'):
            self.assertIn(needle, html, f'查看页缺少: {needle}')

    def test_save_records_overwrites_whole_ledger(self):
        append_record(self.path, LedgerRecord(name='old'))
        save_records(self.path, [LedgerRecord(name='new')])
        got = load_records(self.path)
        self.assertEqual([r.name for r in got], ['new'])

    def test_normalize_and_view_paths(self):
        self.assertEqual(polyglot_ledger.normalize_ledger_path('a.html'), 'a.json')
        self.assertEqual(polyglot_ledger.normalize_ledger_path('a.JSON'), 'a.JSON')
        self.assertEqual(polyglot_ledger.html_view_path('d\\a.html'), 'd\\a.html')
        self.assertEqual(polyglot_ledger.html_view_path('d\\a.json'), 'd\\a.html')

    def test_default_ledger_path_uses_ledger_filename(self):
        self.assertEqual(os.path.basename(polyglot_ledger.default_ledger_path()),
                         polyglot_ledger.LEDGER_FILENAME)
        self.assertTrue(polyglot_ledger.default_ledger_path().endswith('.json'))

    def test_config_path_roundtrip(self):
        from unittest import mock
        with mock.patch.object(polyglot_ledger, 'default_ledger_path',
                               return_value=self.path):
            self.assertIsNone(polyglot_ledger.load_configured_path())
            other = os.path.join(self.tmpdir, 'other.json')
            create_ledger(other)
            polyglot_ledger.save_configured_path(other)
            self.assertEqual(polyglot_ledger.load_configured_path(), other)
            self.assertEqual(polyglot_ledger.resolve_ledger_path(), other)

    def test_config_stores_normalized_json_path(self):
        from unittest import mock
        with mock.patch.object(polyglot_ledger, 'default_ledger_path',
                               return_value=self.path):
            create_ledger(self.path)
            polyglot_ledger.save_configured_path(self.view)  # 传 .html
            self.assertEqual(polyglot_ledger.load_configured_path(), self.path)

    def test_resolve_falls_back_when_configured_path_gone(self):
        from unittest import mock
        with mock.patch.object(polyglot_ledger, 'default_ledger_path',
                               return_value=self.path):
            polyglot_ledger.save_configured_path(
                os.path.join(self.tmpdir, 'deleted.json'))
            # 记住的路径已不存在 -> 回退到默认位置
            self.assertEqual(polyglot_ledger.resolve_ledger_path(), self.path)


class TestLedgerCLI(unittest.TestCase):
    """守护 CLI --ledger 记账行为。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # 避免测试往仓库目录写 ledger_config.json
        patcher = mock.patch.object(polyglot_ledger, 'save_configured_path')
        self.mock_save_cfg = patcher.start()
        self.addCleanup(patcher.stop)

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

    def _build(self, extra_args=()):
        outer = self._make_file('outer.bin', b'OUTER' * 50)
        rar = self._make_file('data.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'output.bin')
        ledger = os.path.join(self.tmpdir, '台账.json')
        argv = ['polyglot_build.py', outer, rar, '-o', out, '-q',
                '--ledger', ledger] + list(extra_args)
        code, _o, err = self._run_main(argv)
        self.assertEqual(code, 0, err)
        return ledger, out

    def test_ledger_records_build(self):
        ledger, out = self._build()
        recs = load_records(ledger)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].filename, os.path.basename(out))
        self.assertTrue(recs[0].size)
        self.assertTrue(recs[0].date)
        self.assertEqual(recs[0].rar_password, '', '静默模式不应询问密码')

    def test_ledger_fields_from_args(self):
        ledger, _out = self._build([
            '--ledger-name', '测试资源', '--ledger-netdisk', '夸克网盘',
            '--ledger-location', '/资源/2026', '--note', '备注文本'])
        rec = load_records(ledger)[0]
        self.assertEqual(rec.name, '测试资源')
        self.assertEqual(rec.netdisk, '夸克网盘')
        self.assertEqual(rec.netdisk_path, '/资源/2026')
        self.assertEqual(rec.note, '备注文本')

    def test_ledger_name_defaults_to_filename(self):
        ledger, out = self._build()
        self.assertEqual(load_records(ledger)[0].name,
                         os.path.basename(out))

    def test_no_ledger_flag_leaves_no_file(self):
        outer = self._make_file('o.bin', b'OUTER' * 50)
        rar = self._make_file('r.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'o_out.bin')
        code, _o, err = self._run_main(
            ['polyglot_build.py', outer, rar, '-o', out, '-q'])
        self.assertEqual(code, 0, err)
        leftovers = [n for n in os.listdir(self.tmpdir)
                     if n.endswith(('.html', '.json'))]
        self.assertEqual(leftovers, [], '未指定 --ledger 时不应产生台账文件')

    def test_legacy_html_ledger_arg_migrates(self):
        """--ledger 传旧版 .html 路径: 应规范化为 .json 并生成查看页。"""
        outer = self._make_file('o3.bin', b'OUTER' * 50)
        rar = self._make_file('r3.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'o3_out.bin')
        legacy = os.path.join(self.tmpdir, '台账.html')
        code, _o, err = self._run_main(
            ['polyglot_build.py', outer, rar, '-o', out, '-q',
             '--ledger', legacy, '--ledger-name', '旧路径'])
        self.assertEqual(code, 0, err)
        json_path = os.path.join(self.tmpdir, '台账.json')
        self.assertTrue(os.path.isfile(json_path), '应写入规范化后的 JSON')
        self.assertEqual(load_records(json_path)[0].name, '旧路径')

    def test_ledger_write_failure_does_not_break_build(self):
        # 台账路径指向一个目录 -> 写入失败, 但构建仍应成功
        outer = self._make_file('o2.bin', b'OUTER' * 50)
        rar = self._make_file('r2.rar', b'RAR' * 50)
        out = os.path.join(self.tmpdir, 'o2_out.bin')
        bad_ledger = os.path.join(self.tmpdir, 'adir.json')
        os.makedirs(bad_ledger)
        code, _o, err = self._run_main(
            ['polyglot_build.py', outer, rar, '-o', out, '-q',
             '--ledger', bad_ledger])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out))


class TestLedgerGUI(unittest.TestCase):
    """守护 GUI 台账入口与记账对话框。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _root(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        self.addCleanup(root.destroy)
        return root

    def test_dialog_save_builds_record(self):
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='a.mp4', size='10 MB',
                                 date='2026-09-03 10:00')
        dlg._vars['name'].set('  资源A  ')
        dlg._vars['netdisk'].set('百度网盘')
        dlg._vars['netdisk_path'].set('/dir')
        dlg._vars['rar_password'].set('pw123')
        dlg._on_save()
        self.assertIsNotNone(dlg.record)
        self.assertEqual(dlg.record.name, '资源A', '应去除首尾空白')
        self.assertEqual(dlg.record.filename, 'a.mp4')
        self.assertEqual(dlg.record.size, '10 MB')
        self.assertEqual(dlg.record.date, '2026-09-03 10:00')
        self.assertEqual(dlg.record.rar_password, 'pw123')

    def test_dialog_cancel_yields_none(self):
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='b.mp4', size='1 MB', date='x')
        dlg._on_cancel()
        self.assertIsNone(dlg.record)

    def test_dialog_prefills_build_output_as_editable(self):
        """构建产物三项应为可编辑输入框的预填值, 而非只读展示。"""
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='a.mp4', size='10 MB',
                                 date='2026-09-03 10:00')
        self.assertEqual(dlg._vars['filename'].get(), 'a.mp4')
        self.assertEqual(dlg._vars['size'].get(), '10 MB')
        self.assertEqual(dlg._vars['date'].get(), '2026-09-03 10:00')
        # 可改: 手改文件名后应被保存
        dlg._vars['filename'].set('renamed.mp4')
        dlg._on_save()
        self.assertEqual(dlg.record.filename, 'renamed.mp4')

    def test_dialog_manual_add_can_fill_filename(self):
        """从管理窗口手动新增 (无构建产物) 时, 文件名应可填写并保存。"""
        root = self._root()
        dlg = LedgerRecordDialog(root, date='2026-09-03 12:00')
        self.assertEqual(dlg._vars['filename'].get(), '')
        dlg._vars['name'].set('手动录入的资源')
        dlg._vars['filename'].set('manual.mkv')
        dlg._vars['size'].set('3.3 GB')
        dlg._on_save()
        self.assertEqual(dlg.record.filename, 'manual.mkv')
        self.assertEqual(dlg.record.size, '3.3 GB')
        self.assertEqual(dlg.record.name, '手动录入的资源')
        self.assertEqual(dlg.record.date, '2026-09-03 12:00')

    def test_dialog_untouched_fields_stay_empty(self):
        """未填写的字段必须是空字符串, 不能被存成提示文字。"""
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='a.mp4', size='1 MB',
                                 date='2026-09-03 10:00')
        dlg._vars['name'].set('只填名称')
        dlg._on_save()
        for key in ('netdisk_path', 'share_link', 'share_code',
                    'rar_password', 'note', 'netdisk'):
            self.assertEqual(getattr(dlg.record, key), '',
                             f'{key} 不应被填入提示文字')

    def test_dialog_date_autofilled_when_left_empty(self):
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='a.mp4', size='1 MB')
        dlg._on_save()
        self.assertTrue(dlg.record.date, '记录时间留空时应自动填当前时间')

    def test_dialog_edit_prefills_every_field(self):
        root = self._root()
        rec = LedgerRecord(name='旧名', filename='old.mp4', size='2 MB',
                           date='2026-01-01 08:00', netdisk='夸克网盘',
                           netdisk_path='/旧路径', share_link='https://x',
                           share_code='c1', rar_password='pw', note='备注')
        dlg = LedgerRecordDialog(root, record=rec)
        for key in ('name', 'filename', 'size', 'date', 'netdisk',
                    'netdisk_path', 'share_link', 'share_code',
                    'rar_password', 'note'):
            self.assertEqual(dlg._vars[key].get(), getattr(rec, key),
                             f'编辑时 {key} 应回填')
        dlg._vars['filename'].set('new.mp4')
        dlg._on_save()
        self.assertEqual(dlg.record.filename, 'new.mp4')
        self.assertEqual(dlg.record.rar_password, 'pw', '未改动的字段应保留')

    def test_entry_value_treats_placeholder_as_empty(self):
        """PlaceholderEntry 会把占位文字写进变量, _entry_value 必须识别并视为空。"""
        import tkinter as tk
        root = self._root()
        var = tk.StringVar(master=root)
        entry = polyglot_gui.PlaceholderEntry(root, placeholder='选择文件',
                                              textvariable=var)
        self.assertEqual(var.get(), '选择文件', '前提: 占位文字确实会写进变量')
        self.assertEqual(PolyglotGUI._entry_value(var, entry), '',
                         '仅显示占位提示时应视为未填写')
        entry.set('D:/a.mp4')
        self.assertEqual(PolyglotGUI._entry_value(var, entry), 'D:/a.mp4')

    def test_start_build_warns_when_no_file_selected(self):
        """未选文件就点开始构建: 应提示"请选择外层文件"而非"文件不存在"。"""
        root = self._root()
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        with mock.patch.object(messagebox, 'showwarning') as warn, \
                mock.patch.object(messagebox, 'showerror') as err:
            gui._start_build()
        warn.assert_called_once()
        self.assertIn('请选择外层文件', warn.call_args[0][1])
        err.assert_not_called()

    def test_dialog_writes_to_ledger_file(self):
        root = self._root()
        dlg = LedgerRecordDialog(root, filename='c.mp4', size='2 MB',
                                 date='2026-09-03 11:00')
        dlg._vars['name'].set('资源C')
        dlg._vars['rar_password'].set('pwC')
        dlg._on_save()
        path = os.path.join(self.tmpdir, '台账.json')
        append_record(path, dlg.record)
        recs = load_records(path)
        self.assertEqual(recs[0].name, '资源C')
        self.assertEqual(recs[0].rar_password, 'pwC')

    def test_main_window_has_ledger_button(self):
        root = self._root()
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        self.assertTrue(hasattr(gui, 'ledger_btn'))
        self.assertTrue(hasattr(gui, '_open_ledger'))
        self.assertTrue(hasattr(gui, '_prompt_ledger_record'))

    def test_ledger_button_sits_in_header_row(self):
        """台账按钮应在标题行右侧, 不再挤在构建按钮行。"""
        root = self._root()
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        info = gui.ledger_btn.grid_info()
        # grid_info 返回值在不同 Tk 版本可能是 int 或 str, 统一转字符串比较
        self.assertEqual(str(info['row']), '0', '应位于标题行 (row 0)')
        self.assertEqual(str(info['column']), '1', '应右对齐于标题行第二列')
        # 台账按钮与构建按钮不同容器: 不再挤在构建按钮行里
        # (注: grid_info 的 row 是相对父容器的, 不能直接比较行号)
        self.assertIsNot(gui.ledger_btn.master, gui.build_btn.master,
                         '台账按钮应与构建按钮分属不同容器')
        self.assertIsNot(gui.ledger_btn.master, gui.cancel_btn.master)

    def test_ledger_button_uses_high_contrast_colors(self):
        """台账按钮用浅蓝底+蓝字, 不再是对比不足的中灰底白字。"""
        root = self._root()
        gui = PolyglotGUI(root)
        root.update_idletasks()
        self.assertEqual(gui.ledger_btn._bg, polyglot_gui.C_LEDGER)
        self.assertEqual(gui.ledger_btn._fg, polyglot_gui.C_PRIMARY)
        self.assertNotEqual(gui.ledger_btn._bg, '#8E8E93')

    def test_on_success_offers_ledger_recording(self):
        root = self._root()
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        out = os.path.join(self.tmpdir, 'done.bin')
        with open(out, 'wb') as f:
            f.write(b'X' * 100)
        with mock.patch.object(messagebox, 'showinfo'), \
                mock.patch.object(messagebox, 'askyesno', return_value=True), \
                mock.patch.object(gui, '_prompt_ledger_record') as prompt:
            gui._on_success(out)
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args[0][0], out)

    def test_on_success_skip_ledger_when_declined(self):
        root = self._root()
        root.geometry('880x700')
        gui = PolyglotGUI(root)
        root.update_idletasks()
        out = os.path.join(self.tmpdir, 'done2.bin')
        with open(out, 'wb') as f:
            f.write(b'X' * 100)
        with mock.patch.object(messagebox, 'showinfo'), \
                mock.patch.object(messagebox, 'askyesno', return_value=False), \
                mock.patch.object(gui, '_prompt_ledger_record') as prompt:
            gui._on_success(out)
        prompt.assert_not_called()


class TestLedgerManagerGUI(unittest.TestCase):
    """守护台账管理窗口的列表/搜索/新增/编辑/删除。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, '资源台账.json')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _root(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f'无可用显示环境, 跳过 GUI 测试: {e}')
        self.addCleanup(root.destroy)
        return root

    def _manager(self, records=()):
        for r in records:
            append_record(self.path, r)
        mgr = LedgerManagerDialog(self._root(), self.path)
        self.addCleanup(mgr.destroy)
        return mgr

    @staticmethod
    def _fake_dialog(result):
        """记账对话框替身: 不弹窗, 直接给出预设结果。"""
        class _Fake:
            def __init__(self, parent, **kw):
                self.record = result

            def grab_set(self):
                pass
        return _Fake

    def test_lists_all_records(self):
        mgr = self._manager([LedgerRecord(name='A', filename='a.mp4'),
                             LedgerRecord(name='B', filename='b.mp4')])
        self.assertEqual(len(mgr.tree.get_children()), 2)
        self.assertIn('共 2 条', mgr._status_lbl.cget('text'))

    def test_search_filters_rows(self):
        mgr = self._manager([LedgerRecord(name='游戏包', netdisk='百度网盘'),
                             LedgerRecord(name='电影', netdisk='夸克网盘')])
        mgr._query.set('夸克')
        self.assertEqual(len(mgr.tree.get_children()), 1)
        mgr._query.set('')
        self.assertEqual(len(mgr.tree.get_children()), 2)
        mgr._query.set('zzz')
        self.assertEqual(len(mgr.tree.get_children()), 0)

    def test_add_appends_and_persists(self):
        mgr = self._manager([LedgerRecord(name='old')])
        new = LedgerRecord(name='新增资源', filename='n.mp4', size='1 MB',
                           date='2026-09-03 10:00', rar_password='pw')
        with mock.patch('polyglot_gui.LedgerRecordDialog', self._fake_dialog(new)), \
                mock.patch.object(mgr, 'wait_window'):
            mgr._on_add()
        recs = load_records(self.path)
        self.assertEqual([r.name for r in recs], ['old', '新增资源'])
        self.assertEqual(recs[1].rar_password, 'pw')

    def test_edit_updates_selected_record(self):
        mgr = self._manager([LedgerRecord(name='A', filename='a.mp4'),
                             LedgerRecord(name='B', filename='b.mp4')])
        mgr.tree.selection_set(mgr.tree.get_children()[1])
        edited = LedgerRecord(name='B-已改', filename='b2.mp4', size='9 MB',
                              date='2026-09-03 11:00', netdisk='阿里云盘',
                              rar_password='newpw')
        with mock.patch('polyglot_gui.LedgerRecordDialog',
                        self._fake_dialog(edited)), \
                mock.patch.object(mgr, 'wait_window'):
            mgr._on_edit()
        recs = load_records(self.path)
        self.assertEqual([r.name for r in recs], ['A', 'B-已改'])
        self.assertEqual(recs[1].netdisk, '阿里云盘')
        self.assertEqual(recs[1].rar_password, 'newpw')

    def test_edit_without_selection_is_noop(self):
        mgr = self._manager([LedgerRecord(name='A')])
        with mock.patch.object(messagebox, 'showinfo') as info:
            mgr._on_edit()
        info.assert_called_once()
        self.assertEqual(len(load_records(self.path)), 1)

    def test_delete_removes_selected_record(self):
        mgr = self._manager([LedgerRecord(name='A'), LedgerRecord(name='B')])
        mgr.tree.selection_set(mgr.tree.get_children()[0])
        with mock.patch.object(messagebox, 'askyesno', return_value=True):
            mgr._on_delete()
        recs = load_records(self.path)
        self.assertEqual([r.name for r in recs], ['B'])
        self.assertEqual(len(mgr.tree.get_children()), 1)

    def test_delete_cancelled_keeps_record(self):
        mgr = self._manager([LedgerRecord(name='A')])
        mgr.tree.selection_set(mgr.tree.get_children()[0])
        with mock.patch.object(messagebox, 'askyesno', return_value=False):
            mgr._on_delete()
        self.assertEqual(len(load_records(self.path)), 1)

    def test_edit_dialog_skipped_keeps_record(self):
        mgr = self._manager([LedgerRecord(name='A')])
        mgr.tree.selection_set(mgr.tree.get_children()[0])
        with mock.patch('polyglot_gui.LedgerRecordDialog',
                        self._fake_dialog(None)), \
                mock.patch.object(mgr, 'wait_window'):
            mgr._on_edit()
        self.assertEqual(load_records(self.path)[0].name, 'A')


class TestCompressEncoderSelection(unittest.TestCase):
    """守护压缩提速改造: preset 档位、硬件编码器探测与回退、音频直拷、ETA。"""

    def setUp(self):
        polyglot_build._HW_CACHE.clear()
        self.addCleanup(polyglot_build._HW_CACHE.clear)
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    # ---------- preset 档位 ----------
    def test_preset_defined_for_every_quality_tier(self):
        self.assertEqual(set(polyglot_build.VIDEO_PRESET), set(VIDEO_QUALITY))
        # 不再写死 medium: 各档均为偏速度的 preset
        self.assertNotIn('medium', polyglot_build.VIDEO_PRESET.values())

    # ---------- 音频参数 ----------
    def test_audio_args_variants(self):
        self.assertEqual(polyglot_build._audio_args(''), ['-an'])
        self.assertEqual(polyglot_build._audio_args('aac'), ['-c:a', 'copy'])
        self.assertEqual(polyglot_build._audio_args('mp3'),
                         ['-c:a', 'aac', '-b:a', '128k'])
        self.assertEqual(polyglot_build._audio_args(None),
                         ['-c:a', 'aac', '-b:a', '128k'])

    # ---------- 硬件编码器探测 ----------
    def test_detect_hw_encoder_returns_none_when_ffmpeg_unusable(self):
        with mock.patch.object(polyglot_build.subprocess, 'run',
                               side_effect=OSError('not found')):
            self.assertIsNone(polyglot_build.detect_hw_encoder('/no/ffmpeg'))

    def test_detect_hw_encoder_skips_listed_but_unusable(self):
        """nvenc 被列出但实测失败 (无 N 卡) -> 应跳到 amf。"""
        def fake_run(cmd, **kw):
            class R:
                returncode = 0
                stdout = b''
                stderr = b''
            r = R()
            if '-encoders' in cmd:
                r.stdout = (' V....D h264_nvenc\n V....D h264_qsv\n'
                            ' V....D h264_amf\n V....D libx264\n').encode()
            elif 'h264_nvenc' in cmd or 'h264_qsv' in cmd:
                r.returncode = 1          # 硬件不可用
            return r

        with mock.patch.object(polyglot_build.subprocess, 'run', fake_run):
            self.assertEqual(polyglot_build.detect_hw_encoder('/ff'), 'h264_amf')
        # 结果已缓存: 再次调用不重复探测
        with mock.patch.object(polyglot_build.subprocess, 'run',
                               side_effect=AssertionError('不应再探测')):
            self.assertEqual(polyglot_build.detect_hw_encoder('/ff'), 'h264_amf')

    def test_detect_hw_encoder_none_when_all_fail(self):
        def fake_run(cmd, **kw):
            class R:
                returncode = 1 if '-encoders' not in cmd else 0
                stdout = b' V....D h264_nvenc\n' if '-encoders' in cmd else b''
                stderr = b''
            return R()

        with mock.patch.object(polyglot_build.subprocess, 'run', fake_run):
            self.assertIsNone(polyglot_build.detect_hw_encoder('/ff2'))

    # ---------- compress_video 组装的命令 ----------
    def _capture_cmd(self, quality='medium', hw=None, audio_codec=None,
                     use_hw=False):
        """跑一次 compress_video (均 mock), 捕获传给 ffmpeg 的命令行。"""
        captured = {}

        class FakeProc:
            def __init__(self, cmd, **kw):
                captured['cmd'] = cmd
                self.stdout = iter([b'progress=end\n'])
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

        dst = os.path.join(self.tmpdir, 'out.mp4')
        with mock.patch('polyglot_build.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_build.detect_hw_encoder',
                           return_value=hw) as det, \
                mock.patch('polyglot_build._find_ffprobe', return_value=None), \
                mock.patch('polyglot_build._probe_audio_codec',
                           return_value=audio_codec), \
                mock.patch.object(polyglot_build.subprocess, 'Popen', FakeProc), \
                mock.patch.object(polyglot_build.os.path, 'exists',
                                  return_value=False):
            compress_video('in.mp4', dst, quality=quality, use_hw=use_hw)
        captured['detect_calls'] = det.call_count
        return captured['cmd'], captured['detect_calls']

    def test_cmd_uses_hw_encoder_when_opted_in(self):
        cmd, _calls = self._capture_cmd(hw='h264_amf', use_hw=True)
        self.assertIn('h264_amf', cmd)
        self.assertNotIn('libx264', cmd)
        self.assertIn('-quality', cmd)

    def test_default_never_probes_hardware(self):
        """默认走 CPU: 不应去探测硬件编码器 (避免无意义的启动开销)。"""
        cmd, calls = self._capture_cmd(hw='h264_amf', use_hw=False)
        self.assertEqual(calls, 0, '默认不应调用 detect_hw_encoder')
        self.assertIn('libx264', cmd)
        self.assertNotIn('h264_amf', cmd)

    def test_cmd_falls_back_to_libx264_with_tier_preset(self):
        cmd, _calls = self._capture_cmd(quality='low', hw=None)
        self.assertIn('libx264', cmd)
        self.assertIn('ultrafast', cmd)
        self.assertNotIn('medium', cmd, '不应再写死 medium preset')

    def test_hw_requested_but_unavailable_falls_back_with_notice(self):
        """开了硬件编码但探测不到: 回退 libx264 并告知用户。"""
        msgs = []

        def cb(phase, cur, total, msg):
            msgs.append(msg)

        class FakeProc:
            def __init__(self, cmd, **kw):
                self.cmd = cmd
                self.stdout = iter([b'progress=end\n'])
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

        with mock.patch('polyglot_build.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_build.detect_hw_encoder',
                           return_value=None), \
                mock.patch('polyglot_build._find_ffprobe', return_value=None), \
                mock.patch('polyglot_build._probe_audio_codec',
                           return_value=None), \
                mock.patch.object(polyglot_build.subprocess, 'Popen', FakeProc), \
                mock.patch.object(polyglot_build.os.path, 'exists',
                                  return_value=False):
            compress_video('in.mp4', os.path.join(self.tmpdir, 'fb.mp4'),
                           callback=cb, use_hw=True)
        text = ' '.join(msgs)
        self.assertIn('未探测到可用的硬件编码器', text)
        self.assertIn('libx264', text)

    def test_cmd_copies_audio_when_source_is_aac(self):
        cmd, _c = self._capture_cmd(hw=None, audio_codec='aac')
        i = cmd.index('-c:a')
        self.assertEqual(cmd[i + 1], 'copy')
        self.assertNotIn('128k', cmd)

    def test_cmd_drops_audio_when_source_has_none(self):
        cmd, _c = self._capture_cmd(hw=None, audio_codec='')
        self.assertIn('-an', cmd)
        self.assertNotIn('-c:a', cmd)

    def test_cmd_reencodes_audio_for_other_codec(self):
        cmd, _c = self._capture_cmd(hw=None, audio_codec='mp3')
        self.assertIn('128k', cmd)

    def test_info_callback_reports_encoder(self):
        msgs = []

        def cb(phase, cur, total, msg):
            msgs.append((phase, msg))

        class FakeProc:
            def __init__(self, cmd, **kw):
                self.stdout = iter([b'progress=end\n'])
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

        with mock.patch('polyglot_build.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_build.detect_hw_encoder',
                           return_value='h264_qsv'), \
                mock.patch('polyglot_build._find_ffprobe', return_value=None), \
                mock.patch('polyglot_build._probe_audio_codec',
                           return_value=None), \
                mock.patch.object(polyglot_build.subprocess, 'Popen', FakeProc), \
                mock.patch.object(polyglot_build.os.path, 'exists',
                                  return_value=False):
            compress_video('in.mp4', os.path.join(self.tmpdir, 'o.mp4'),
                           callback=cb, use_hw=True)
        info = ' '.join(m for p, m in msgs if p == 'info')
        self.assertIn('h264_qsv', info)
        self.assertIn('硬件加速', info)

    # ---------- ETA ----------
    def test_format_eta(self):
        self.assertEqual(polyglot_build._format_eta(30), '30 秒')
        self.assertEqual(polyglot_build._format_eta(90), '1 分 30 秒')
        self.assertEqual(polyglot_build._format_eta(600), '10 分')
        self.assertEqual(polyglot_build._format_eta(3660), '1 小时 1 分')

    def test_estimate_eta_needs_enough_samples(self):
        # 墙钟不足 3 秒 -> 不给估计
        self.assertIsNone(polyglot_build._estimate_eta(1.0, 100.0, time.time()))
        # 已跑 10 秒完成 5 秒媒体, 总长 100 秒 -> 剩余约 190 秒
        eta = polyglot_build._estimate_eta(5.0, 100.0, time.time() - 10)
        self.assertIsNotNone(eta)
        self.assertIn('分', eta)

    def test_progress_message_contains_eta(self):
        class FakeProc:
            def __init__(self, cmd, **kw):
                self.stdout = iter([b'out_time=00:00:05.000000\n',
                                    b'progress=end\n'])
                self.returncode = 0

            def wait(self, *a, **k):
                return 0

        msgs = []

        def cb(phase, cur, total, msg):
            if phase == 'compress':
                msgs.append(msg)

        with mock.patch('polyglot_build.find_ffmpeg', return_value='/ff'), \
                mock.patch('polyglot_build.detect_hw_encoder', return_value=None), \
                mock.patch('polyglot_build._find_ffprobe', return_value='/fp'), \
                mock.patch('polyglot_build._probe_duration', return_value=10.0), \
                mock.patch('polyglot_build._probe_audio_codec',
                           return_value=None), \
                mock.patch('polyglot_build._estimate_eta', return_value='2 分'), \
                mock.patch.object(polyglot_build.subprocess, 'Popen', FakeProc), \
                mock.patch.object(polyglot_build.os.path, 'exists',
                                  return_value=False):
            compress_video('in.mp4', os.path.join(self.tmpdir, 'o2.mp4'),
                           callback=cb)
        self.assertTrue(msgs)
        self.assertIn('50%', msgs[0])
        self.assertIn('预计还需 2 分', msgs[0])


if __name__ == '__main__':
    unittest.main()
