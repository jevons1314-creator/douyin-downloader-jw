import concurrent.futures
import hashlib
import io
import http.server
import importlib.util
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('download_pause', Path(__file__).parents[1] / 'scripts/download_douyin.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PauseResumeTests(unittest.TestCase):
    def test_resume_after_transfer_complete_only_probes_then_verifies(self):
        payload = b'\x00\x00\x00\x18ftypisom' + b'a' * 65524
        class Response(io.BytesIO):
            headers = {'Content-Range': f'bytes 0-65535/{len(payload)}'}
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / '001_校验暂停_123.mp4.part'
            partial.write_bytes(payload)
            item = {'sequence': 1, 'title': '校验暂停', 'url': 'https://www.douyin.com/video/123',
                    'video_id': '123', 'expected_title': '校验暂停', 'resume_requested': True,
                    'total_bytes': len(payload), 'download_engine': 'urllib', 'transfer_complete': True}
            with patch.object(module, 'resolve_video', return_value=('http://localhost/video', 'test', '校验暂停')), \
                 patch.object(module, 'find_aria2', return_value=None), \
                 patch.object(module.urllib.request, 'urlopen', side_effect=[Response(payload), AssertionError('完整断点不应发送越界 Range')]) as network:
                path = module.download_one(item, Path(folder), item.update)
                self.assertEqual(network.call_count, 1)
            self.assertEqual(path.read_bytes(), payload)
            self.assertFalse(partial.exists())

    def test_standard_library_pause_and_resume_keeps_exact_bytes(self):
        self.assert_real_engine_pause_and_resume(None)

    def test_installed_aria2_pause_and_resume_keeps_exact_bytes(self):
        engine = module.find_aria2()
        if engine is None:
            self.skipTest('此平台未使用或未安装 aria2，标准库路径另行实测')
        self.assert_real_engine_pause_and_resume(engine)

    def assert_real_engine_pause_and_resume(self, engine):
        # A local byte fixture tests transfer integrity, not MP4 playability.
        payload = b'\x00\x00\x00\x18ftypisom' + bytes(range(256)) * 65536
        ranges = []
        updates = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
                start = int(match[1]) if match else 0
                end = min(int(match[2]) if match and match[2] else len(payload) - 1, len(payload) - 1)
                ranges.append((start, end))
                self.send_response(206 if match else 200)
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                try:
                    for offset in range(start, end + 1, 32768):
                        self.wfile.write(payload[offset:min(offset + 32768, end + 1)])
                        self.wfile.flush()
                        time.sleep(.05 if engine else .025)
                except ConnectionError:
                    pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                item = {'sequence': 1, 'title': '本地传输校验', 'url': 'https://www.douyin.com/video/123',
                        'video_id': '123', 'expected_title': '本地传输校验'}
                stop = threading.Event()
                partial = Path(folder) / '001_本地传输校验_123.mp4.part'
                url = f'http://127.0.0.1:{server.server_port}/fixture'
                def progress(values):
                    updates.append(values)
                    item.update(values)
                with patch.object(module, 'resolve_video', return_value=(url, 'test', '本地传输校验')), \
                     patch.object(module, 'find_aria2', return_value=engine):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(module.download_one, item, Path(folder), progress, stop)
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if partial.exists() and partial.stat().st_size >= 65536:
                                with partial.open('rb') as f:
                                    if f.read(65536) == payload[:65536]:
                                        break
                            time.sleep(.02)
                        stop.set()
                        with self.assertRaises(module.DownloadPaused):
                            future.result(timeout=10)
                    self.assertTrue(partial.exists())
                    self.assertEqual(item['download_engine'], 'aria2' if engine else 'urllib')
                    if engine:
                        self.assertTrue(Path(str(partial) + '.aria2').exists())
                    original_prefix = partial.read_bytes()[:65536]
                    self.assertEqual(original_prefix, payload[:65536])
                    item['resume_requested'] = True
                    path = module.download_one(item, Path(folder), progress, threading.Event())
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(payload).digest())
                self.assertFalse(partial.exists())
                self.assertTrue(any(start > 0 for start, _ in ranges))
                if engine:
                    self.assertTrue(any('transferred' in update for update in updates), 'aria2 必须报告传输中进度')
        finally:
            server.shutdown()
            server.server_close()

    def test_pause_before_start_sends_no_request(self):
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder, patch.object(module, 'resolve_video') as resolve:
            with self.assertRaises(module.DownloadPaused):
                module.download_one({'sequence': 1, 'title': '暂停', 'video_id': '123', 'url': 'x'}, Path(folder), stop_event=stop)
            resolve.assert_not_called()

    def test_resume_rejects_changed_source_without_touching_partial(self):
        original = b'\x00\x00\x00\x18ftypisom' + b'a' * 65524
        different = original[:12] + b'b' * 65524
        class Response(io.BytesIO):
            headers = {'Content-Range': f'bytes 0-65535/{len(original)}'}
        with tempfile.TemporaryDirectory() as folder:
            partial = Path(folder) / '001_原片_123.mp4.part'
            partial.write_bytes(original)
            control = Path(str(partial) + '.aria2')
            control.write_bytes(b'preserve control')
            item = {'sequence': 1, 'title': '原片', 'url': 'https://www.douyin.com/video/123', 'video_id': '123',
                    'expected_title': '原片', 'resume_requested': True, 'total_bytes': len(original)}
            with patch.object(module, 'resolve_video', return_value=('http://localhost/video', 'test', '原片')), \
                    patch.object(module.urllib.request, 'urlopen', return_value=Response(different)), \
                    patch.object(module.subprocess, 'Popen') as engine:
                with self.assertRaisesRegex(RuntimeError, '续传来源与原文件不一致'):
                    module.download_one(item, Path(folder))
                engine.assert_not_called()
            self.assertEqual(partial.read_bytes(), original)
            self.assertEqual(control.read_bytes(), b'preserve control')


if __name__ == '__main__':
    unittest.main()
