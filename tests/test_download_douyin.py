import csv
import io
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "download_douyin.py"
spec = importlib.util.spec_from_file_location("download_douyin", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FilenameTests(unittest.TestCase):
    def test_long_chinese_title_fits_filesystem_filename_limit(self):
        name = module.output_template(1, '中文标题' * 30, '1234567890123456789')
        self.assertLessEqual(len(name.encode('utf-8')), 240)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / name).write_bytes(b'test')

    def test_macos_finds_renamed_motrix_bundle(self):
        engine = Path('/Applications/下载器Motrix.app/Contents/Resources/engine/aria2c')
        with patch.object(module.shutil, 'which', return_value=None), \
             patch.object(module.Path, 'glob', return_value=iter([engine])), \
             patch.object(module.Path, 'is_file', autospec=True, side_effect=lambda p: p == engine):
            self.assertEqual(module.find_aria2('darwin'), str(engine))

    def test_configuration_is_requested_over_https(self):
        self.assertEqual(module.CONFIG_URL, 'https://i.rcuts.com/update/247')

    def test_windows_uses_standard_library_engine_for_reliable_pause(self):
        with patch.object(module.shutil, 'which', return_value='C:/aria2c.exe') as which:
            self.assertIsNone(module.find_aria2('win32'))
            which.assert_not_called()

    def test_dashboard_process_options_are_platform_specific(self):
        self.assertEqual(module.dashboard_process_options('darwin'), {'start_new_session': True})
        self.assertIn('creationflags', module.dashboard_process_options('win32'))
        self.assertNotIn('start_new_session', module.dashboard_process_options('win32'))

    def test_short_link_only_follows_douyin_redirects(self):
        with patch.object(module.urllib.request, 'build_opener') as builder:
            builder.return_value.open.return_value.__enter__.return_value.url = 'https://www.douyin.com/video/123?from=share'
            self.assertEqual(module.canonical_source('https://v.douyin.com/abc/'), 'https://www.douyin.com/video/123')
            handler = builder.call_args.args[0]
            with self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')
            with self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, '', {}, 'https://www.iesdouyin.com/other/123')

    def test_short_link_uses_trusted_redirect_id_without_waiting_for_final_page(self):
        with patch.object(module.urllib.request, 'build_opener') as builder:
            redirect_response = io.BytesIO()
            def first_hop(request, timeout):
                handler = builder.call_args.args[0]
                handler.redirect_request(request, redirect_response, 302, '', {},
                                         'https://www.iesdouyin.com/share/video/123/?from_ssr=1')
                raise TimeoutError('The read operation timed out')
            builder.return_value.open.side_effect = first_hop
            try:
                canonical = module.canonical_source('https://v.douyin.com/abc/')
            except TimeoutError:
                self.fail('已取得可信跳转中的作品 ID，不应继续等待最终网页')
            self.assertEqual(canonical, 'https://www.douyin.com/video/123')
            self.assertTrue(redirect_response.closed)

    def test_auto_title_sets_real_filename_before_media_request(self):
        item = {'sequence': 2, 'title': '待获取作品标题', 'video_id': '123',
                'url': 'https://www.douyin.com/video/123', 'auto_title': True}
        updates = []
        class Halt(Exception):
            pass
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(module, 'resolve_video', return_value=('https://example.test/media', 'UA', '亚马逊预算分配 #广告')), patch.object(module.urllib.request, 'urlopen', side_effect=Halt):
                with self.assertRaises(Halt):
                    module.download_one(item, Path(folder), updates.append)
            result = next(u for u in updates if 'file_path' in u)
            self.assertEqual(Path(result['file_path']).name, '002_亚马逊预算分配_123.mp4')
            self.assertFalse(result['auto_title'])
            self.assertFalse(list(Path(folder).iterdir()))

    def test_filename_contains_sequence_title_and_video_id(self):
        self.assertEqual(
            module.output_template(15, "Prime大促/参考价?", "7688274403485928740"),
            "015_Prime大促_参考价_7688274403485928740.mp4",
        )

    def test_title_cannot_escape_output_directory(self):
        name = module.output_template(1, "../../outside", "123")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_video_id_is_required(self):
        with self.assertRaises(ValueError):
            module.output_template(1, "title", "")


class ParserResponseTests(unittest.TestCase):
    def test_source_title_ignores_punctuation_and_trailing_tags(self):
        module.verify_source_title("Prime大促报好了？90%的卖家会栽在参考价上。", "Prime大促报好了？90%的卖家会栽在参考价上。 #primeday")

    def test_wrong_source_title_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "标题不匹配"):
            module.verify_source_title("Prime大促参考价", "亚马逊广告预算分配")

    def test_success_returns_download_url_and_title(self):
        self.assertEqual(
            module.parse_video_response({"code": 0, "video_url": "https://v6-dy.ixigua.com/aweme/v1/play/", "video_name": "Prime参考价"}),
            ("https://v6-dy.ixigua.com/aweme/v1/play/", "Prime参考价"),
        )

    def test_parser_error_is_not_a_download(self):
        with self.assertRaisesRegex(RuntimeError, "解析失败"):
            module.parse_video_response({"code": 1, "msg": "解析失败"})

    def test_success_without_url_is_rejected(self):
        with self.assertRaises(RuntimeError):
            module.parse_video_response({"code": 0, "video_url": ""})


class ManifestTests(unittest.TestCase):
    def test_manifest_id_must_match_source_url(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "batch.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["sequence", "title", "url", "douyin_id"])
                writer.writeheader()
                writer.writerow({"sequence": 15, "title": "Prime", "url": "https://www.douyin.com/video/7688274403485928740", "douyin_id": "123"})
            with self.assertRaises(ValueError):
                module.load_manifest(path)


class AgentCliTests(unittest.TestCase):
    def test_occupied_http_port_does_not_launch_another_dashboard(self):
        error = urllib.error.HTTPError('http://127.0.0.1:9180/api/health', 404, 'Not Found', {}, None)
        try:
            with patch.object(module, 'dashboard_request', side_effect=error), \
                 patch.object(module.subprocess, 'Popen') as launch:
                with self.assertRaises(RuntimeError):
                    module.ensure_dashboard()
                self.assertEqual(launch.call_count, 0)
        finally:
            error.close()

    def test_dashboard_error_preserves_actionable_reason_and_closes_response(self):
        response = io.BytesIO('{"error":"目标文件已存在，请先核对"}'.encode('utf-8'))
        error = urllib.error.HTTPError('http://127.0.0.1:9180/api/tasks', 400, 'Bad Request', {}, response)
        with patch.object(module.urllib.request, 'build_opener') as builder:
            builder.return_value.open.side_effect = error
            try:
                with self.assertRaisesRegex(RuntimeError, '目标文件已存在'):
                    module.dashboard_request('/api/tasks', {})
            finally:
                error.close()
        self.assertTrue(response.closed)

    def test_chinese_errors_use_utf8_when_harness_pipe_is_legacy_encoded(self):
        env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONUTF8='0')
        result = subprocess.run([sys.executable, str(SCRIPT), '--url', 'https://www.douyin.com/user/abc'],
                                env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'UnicodeEncodeError', result.stderr)
        self.assertIn('URL 中未找到', result.stderr.decode('utf-8'))

    def test_share_text_uses_same_queue_as_web_without_pre_resolving(self):
        text = '分享 https://v.douyin.com/abc/\nhttps://www.douyin.com/jingxuan?modal_id=123'
        requests = []
        def request(route, body=None):
            requests.append((route, body))
            if route == '/api/shares':
                return {'batch_id': 'test', 'added': 2, 'skipped': 0}
            return {'tasks': [{'id': 'one', 'sequence': 1, 'status': 'downloaded', 'percent': 100}]}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(sys, 'argv', [str(SCRIPT), '--share-text', text, '--output-dir', folder]), \
             patch.object(module, 'ensure_dashboard'), \
             patch.object(module, 'dashboard_request', side_effect=request), \
             patch.object(module, 'canonical_source') as resolve:
            self.assertEqual(module.main(), 0)
            self.assertEqual(requests[0], ('/api/shares', {'text': text, 'output_dir': str(Path(folder).resolve())}))
            resolve.assert_not_called()


class ParallelDownloadTests(unittest.TestCase):
    def test_two_downloads_overlap(self):
        barrier = threading.Barrier(2)
        items = [{"sequence": i, "title": f"视频{i}", "video_id": str(i)} for i in (1, 2)]
        with tempfile.TemporaryDirectory() as folder:
            def download(item, output_dir):
                barrier.wait(timeout=2)
                path = output_dir / f"{item['sequence']}.mp4"
                path.write_bytes(b"test")
                return path
            with patch.object(module, "download_one", side_effect=download):
                paths = module.download_batch(items, Path(folder), jobs=2)
            self.assertEqual(len(paths), 2)

    def test_failure_does_not_start_more_items(self):
        items = [{"sequence": i, "title": f"视频{i}", "video_id": str(i)} for i in (1, 2, 3)]
        with patch.object(module, "download_one", side_effect=RuntimeError("解析失败")) as download:
            with self.assertRaises(RuntimeError):
                module.download_batch(items, Path("/tmp"), jobs=1)
            self.assertEqual(download.call_count, 1)

    def test_duplicate_output_is_rejected_before_download(self):
        item = {"sequence": 1, "title": "视频", "video_id": "123"}
        with patch.object(module, "download_one") as download:
            with self.assertRaises(ValueError):
                module.download_batch([item, item.copy()], Path("/tmp"), jobs=2)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
