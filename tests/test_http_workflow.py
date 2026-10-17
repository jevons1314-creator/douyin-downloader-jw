"""Exercise the real HTTP queue using a local byte fixture, without RCuts."""

import hashlib
import http.server
import importlib.util
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('http_workflow', Path(__file__).parents[1] / 'dashboard/server.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class HttpWorkflowTests(unittest.TestCase):
    def test_batch_pause_reload_resume_download_and_deduplicate(self):
        payload = b'\x00\x00\x00\x18ftypisom' + bytes(range(256)) * 1024

        class Media(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
                start = int(match[1]) if match else 0
                end = int(match[2]) if match and match[2] else len(payload) - 1
                end = min(end, len(payload) - 1)
                self.send_response(206 if match else 200)
                self.send_header('Content-Length', str(end - start + 1))
                if match:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
                self.end_headers()
                self.wfile.write(payload[start:end + 1])

        media = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Media)
        dashboard = http.server.ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        for server in (media, dashboard):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(route, body=None, host='127.0.0.1:9180'):
            req = urllib.request.Request(f'http://127.0.0.1:{dashboard.server_port}' + route,
                data=json.dumps(body).encode('utf-8') if body is not None else None,
                headers={'Host': host, 'Content-Type': 'application/json'})
            with opener.open(req, timeout=5) as response:
                return json.load(response)

        try:
            with tempfile.TemporaryDirectory() as folder:
                state = Path(folder) / 'state'
                output = Path(folder) / '中文 下载目录'
                store = module.TaskStore(state, parse_interval=0)
                dashboard.store = store
                try:
                    self.assertEqual(request('/api/health')['service'], 'douyin-downloader-jw')
                    with self.assertRaises(urllib.error.HTTPError) as forbidden:
                        request('/api/status', host='foreign.example')
                    self.assertEqual(forbidden.exception.code, 403)
                    forbidden.exception.close()
                    share = '分享 https://v.douyin.com/local/\nhttps://www.douyin.com/jingxuan?modal_id=222\nhttps://www.douyin.com/video/222'
                    batch = request('/api/shares', {'text': share, 'output_dir': str(output)})
                    self.assertEqual((batch['added'], batch['skipped']), (2, 1))
                    ids = [t['id'] for t in store.tasks]
                    self.assertEqual(request('/api/actions', {'action': 'pause', 'scope': 'tasks', 'task_ids': ids})['changed'], 2)
                    store.executor.shutdown()
                    store = module.TaskStore(state, parse_interval=0)
                    dashboard.store = store
                    self.assertEqual([t['id'] for t in store.tasks], ids)
                    self.assertEqual([t['status'] for t in store.tasks], ['paused', 'paused'])
                    request('/api/actions', {'action': 'resume', 'scope': 'tasks', 'task_ids': ids})
                    source = module.downloader.canonical_source
                    media_url = f'http://127.0.0.1:{media.server_port}/fixture'
                    def canonical(url):
                        return 'https://www.douyin.com/video/111' if 'v.douyin.com' in url else source(url)
                    with patch.object(module.downloader, 'canonical_source', side_effect=canonical), \
                         patch.object(module.downloader, 'resolve_video', return_value=(media_url, 'test', '本地流程验收 #标签')), \
                         patch.object(module.downloader, 'find_aria2', return_value=None):
                        store.tick()
                        deadline = time.monotonic() + 10
                        while store.running and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertFalse(store.running)
                    tasks = request('/api/batch/' + batch['batch_id'])['tasks']
                    self.assertEqual([t['status'] for t in tasks], ['downloaded', 'downloaded'])
                    for task in tasks:
                        path = Path(task['file_path'])
                        self.assertEqual(path.parent, output.resolve())
                        self.assertIn('本地流程验收', path.name)
                        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(payload).hexdigest())
                    duplicate = request('/api/shares', {'text': 'https://www.douyin.com/video/111', 'output_dir': str(output)})
                    self.assertEqual((duplicate['added'], duplicate['skipped']), (0, 1))
                finally:
                    store.executor.shutdown()
        finally:
            for server in (dashboard, media):
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
