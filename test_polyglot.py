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
    COMP_STORED, ZIP64_SIZE_THRESHOLD, BuildCancelled,
)
from polyglot_gui import get_output_save_options, _should_follow_outer


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
            build_polyglot(outer, rar, out, callback=cb,
                           method=COMP_STORED, stop_event=stop_event)

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


if __name__ == '__main__':
    unittest.main()
