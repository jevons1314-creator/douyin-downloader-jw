import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dashboard', Path(__file__).with_name('server.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProgressTests(unittest.TestCase):
    def test_process_probe_keeps_current_process_alive(self):
        self.assertTrue(module.process_exists(os.getpid()))

    def test_windows_legacy_process_probe_never_sends_a_signal(self):
        with patch.object(module.platform, 'system', return_value='Windows'), \
             patch.object(module.os, 'kill') as kill, \
             patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout='"python.exe","123","Console","1","9,000 K"\n')):
            self.assertTrue(module.process_exists(123))
            self.assertFalse(module.process_exists(456))
            self.assertEqual(kill.call_count, 0)

    def test_partial_progress_comes_from_log(self):
        log = ''' *** Download Progress Summary as of Fri Oct 16 18:47:20 2026 ***
[#7ebe15 34MiB/85MiB(40%) CN:4 DL:56KiB ETA:15m12s]
FILE: /Downloads/024_示例_7684093570256620810.mp4.part
'''
        result = module.parse_log(log)['7684093570256620810']
        self.assertEqual(result['percent'], 40)
        self.assertEqual(result['speed'], '56KiB/s')
        self.assertEqual(result['eta'], '15m12s')

    def test_two_jobs_keep_their_own_progress(self):
        log = '''[#a 10MiB/20MiB(50%) CN:4 DL:20KiB ETA:10s]
FILE: /Downloads/024_甲_111.mp4.part
[#b 3MiB/12MiB(25%) CN:4 DL:30KiB ETA:30s]
FILE: /Downloads/027_乙_222.mp4.part
[024][完成] /Downloads/024_甲_111.mp4 (20971520 bytes, 10.00 秒)
'''
        result = module.parse_log(log)
        self.assertEqual(result['111']['status'], 'downloaded')
        self.assertEqual(result['111']['percent'], 100)
        self.assertEqual(result['222']['percent'], 25)

    def test_invalid_parallel_does_not_change_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            for value in [0, 5, True, '2']:
                with self.assertRaises(ValueError):
                    module.save_parallel(value, path)
            self.assertFalse(path.exists())
            module.save_parallel(3, path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['parallel'], 3)

    def test_parser_rate_limit_has_its_own_error_type(self):
        with self.assertRaises(module.downloader.RateLimitError) as raised:
            module.downloader.parse_video_response({'code': 1, 'msg': '请求频繁，当前限制：2次/1分钟'})
        self.assertEqual(raised.exception.retry_after, 65)
        with self.assertRaises(RuntimeError) as ordinary:
            module.downloader.parse_video_response({'code': 1, 'msg': '作品不存在'})
        self.assertNotIsInstance(ordinary.exception, module.downloader.RateLimitError)


class QueueTests(unittest.TestCase):
    def test_chinese_state_reloads_under_legacy_windows_default_encoding(self):
        original_open = io.open
        def legacy_open(file, mode='r', buffering=-1, encoding=None, errors=None,
                        newline=None, closefd=True, opener=None):
            if 'b' not in mode and encoding in (None, 'locale'):
                encoding = 'cp1252'
            return original_open(file, mode, buffering, encoding, errors, newline, closefd, opener)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state'
            store = module.TaskStore(state)
            store.submit([{'sequence': 1, 'title': '抖音中文测试', 'url': 'https://www.douyin.com/video/123'}], folder)
            store.executor.shutdown()
            with patch.object(io, 'open', side_effect=legacy_open):
                restored = module.TaskStore(state)
            try:
                self.assertEqual(restored.tasks[0]['title'], '抖音中文测试')
                self.assertEqual(restored.tasks[0]['output_dir'], str(Path(folder).resolve()))
            finally:
                restored.executor.shutdown()

    def test_open_local_reveals_saved_file_or_opens_its_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            try:
                store.submit([{'sequence': 1, 'title': '作品', 'url': 'https://www.douyin.com/video/123'}], folder)
                task = store.tasks[0]
                with patch.object(module.platform, 'system', return_value='Darwin'), patch('subprocess.run') as launch:
                    store.open_local(task['id'])
                    launch.assert_called_once_with(['open', task['output_dir']], check=True)
                    launch.reset_mock()
                    Path(task['file_path']).write_bytes(b'video')
                    store.open_local(task['id'])
                    launch.assert_called_once_with(['open', '-R', task['file_path']], check=True)
                    launch.reset_mock()
                    with self.assertRaises(ValueError):
                        store.open_local('unknown')
                    launch.assert_not_called()
            finally:
                store.executor.shutdown()

    def test_open_local_uses_windows_explorer(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            try:
                store.submit([{'sequence': 1, 'title': '作品', 'url': 'https://www.douyin.com/video/123'}], folder)
                task = store.tasks[0]
                with patch.object(module.platform, 'system', return_value='Windows'), patch('subprocess.run') as launch:
                    store.open_local(task['id'])
                    launch.assert_called_once_with(['explorer', task['output_dir']], check=True)
                    launch.reset_mock()
                    Path(task['file_path']).write_bytes(b'video')
                    store.open_local(task['id'])
                    launch.assert_called_once_with(['explorer', '/select,', task['file_path']], check=True)
            finally:
                store.executor.shutdown()

    def test_preview_resolves_title_without_creating_download_task(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(folder, clock=lambda: 1000)
            try:
                with patch.object(module.downloader, 'canonical_source', return_value='https://www.douyin.com/video/123'), \
                     patch.object(module.downloader, 'resolve_video', return_value=('https://media.example/file', 'UA', '作品标题 #标签')):
                    result = store.preview_share('https://v.douyin.com/abc/')
                self.assertEqual(result, {'status': 'ready', 'url': 'https://www.douyin.com/video/123',
                                          'video_id': '123', 'title': '作品标题 #标签'})
                self.assertEqual(store.next_parse_at, 1031)
                self.assertEqual(store.tasks, [])
            finally:
                store.executor.shutdown()

    def test_preview_marks_existing_id_duplicate_without_using_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(folder, clock=lambda: 1000)
            try:
                store.submit([{'sequence': 1, 'title': '已有作品', 'url': 'https://www.douyin.com/video/123'}], folder)
                with patch.object(module.downloader, 'resolve_video') as parser:
                    result = store.preview_share('https://www.douyin.com/video/123')
                self.assertEqual(result['status'], 'duplicate')
                parser.assert_not_called()
            finally:
                store.executor.shutdown()

    def test_preview_waits_for_shared_parser_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(folder, clock=lambda: 1000)
            try:
                store.set_parser_gate(1031)
                with patch.object(module.downloader, 'resolve_video') as parser:
                    result = store.preview_share('https://www.douyin.com/video/123')
                self.assertEqual(result, {'status': 'wait', 'retry_after': 31})
                parser.assert_not_called()
            finally:
                store.executor.shutdown()

    def test_share_text_dedup_sequence_and_clear_preserve_media(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(folder, parse_interval=0)
            result = store.submit_shares('分享 https://v.douyin.com/abc/，复制打开\nhttps://www.douyin.com/video/123?x=1\nhttps://www.douyin.com/video/123', folder)
            self.assertEqual((result['added'], result['skipped']), (2, 1))
            self.assertEqual(store.submit_shares('https://v.douyin.com/abc/', folder)['added'], 0)
            media = Path(folder) / 'keep.mp4'
            media.write_bytes(b'keep')
            partial = Path(folder) / 'keep.mp4.part'
            partial.write_bytes(b'partial')
            store.tasks[0].update(status='downloaded', file_path=str(media))
            with self.assertRaises(ValueError):
                store.clear('all')
            self.assertEqual(store.clear('completed', True)['removed'], 1)
            self.assertEqual(len(store.tasks), 1)
            store.clear('all', True)
            with self.assertRaises(ValueError):
                store.submit_shares('https://www.douyin.com/video/456', folder)
            store.tick()
            self.assertEqual(store.tasks, [])
            self.assertEqual(media.read_bytes(), b'keep')
            self.assertEqual(partial.read_bytes(), b'partial')
            archived = [json.loads(p.read_text(encoding='utf-8')) for p in (Path(folder) / 'task_history').glob('*.json')]
            self.assertEqual(sum(len(a['tasks']) for a in archived), 2)
            restored = module.TaskStore(folder)
            restored.submit_shares('https://www.douyin.com/video/456', folder)
            self.assertEqual(restored.tasks[0]['sequence'], 3)
            store.executor.shutdown(); restored.executor.shutdown()

    def test_clear_all_waits_for_active_worker(self):
        entered, release = threading.Event(), threading.Event()
        def worker(item, directory, progress, stop_event=None):
            entered.set()
            stop_event.wait(2)
            release.wait(2)
            raise module.downloader.DownloadPaused()
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(folder, worker=worker, parse_interval=0)
            store.submit([self.item(1)], folder)
            store.tick()
            self.assertTrue(entered.wait(1))
            store.clear('all', True)
            store.tick()
            self.assertEqual(len(store.tasks), 1)
            self.assertTrue(store.controls[store.tasks[0]['id']].is_set())
            release.set()
            self.wait_until(lambda: not store.running)
            store.tick()
            self.assertEqual(store.tasks, [])
            store.executor.shutdown()

    def test_share_rejects_non_douyin_and_profile_urls(self):
        for text in ['', 'https://evil.example/video/123', 'https://www.douyin.com/user/abc', 'https://v.douyin.com.evil.test/abc']:
            with self.assertRaises(ValueError):
                module.share_links(text)
        self.assertEqual(module.share_links('看 https://www.douyin.com/video/123。'), ['https://www.douyin.com/video/123'])

    def item(self, number):
        return {'sequence': number, 'title': f'作品{number}',
                'url': f'https://www.douyin.com/video/{number}'}

    def wait_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition())

    def test_bulk_retry_pause_and_resume_preserves_task_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            try:
                store.submit([self.item(1), self.item(2)], folder)
                ids = [t['id'] for t in store.tasks]
                store.tasks[0].update(status='failed', error='timed out')
                self.assertEqual(store.action('retry'), 1)
                self.assertEqual(store.action('pause'), 2)
                self.assertEqual([t['status'] for t in store.tasks], ['paused', 'paused'])
                store.tick()
                self.assertFalse(store.running)
                self.assertEqual(store.action('resume'), 2)
                self.assertEqual([t['status'] for t in store.tasks], ['queued', 'queued'])
                self.assertEqual([t['id'] for t in store.tasks], ids)
                self.assertTrue(store.tasks[0]['action_history'])
            finally:
                store.executor.shutdown()

    def test_active_pause_waits_for_worker_to_stop(self):
        started = threading.Event()
        def worker(task, directory, progress, stop_event=None):
            started.set()
            self.assertTrue(stop_event.wait(2))
            raise module.downloader.DownloadPaused()
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker)
            try:
                store.submit([self.item(1)], folder)
                store.tick()
                self.assertTrue(started.wait(2))
                self.assertEqual(store.action('pause', [store.tasks[0]['id']]), 1)
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'paused')
                self.assertEqual(store.action('resume'), 1)
                self.assertEqual(store.tasks[0]['status'], 'queued')
            finally:
                store.executor.shutdown()

    def test_retry_never_overwrites_completed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            try:
                store.submit([self.item(1)], folder)
                task = store.tasks[0]
                task['status'] = 'failed'
                Path(task['file_path']).write_bytes(b'preserve')
                with self.assertRaises(ValueError):
                    store.action('retry', [task['id']])
                self.assertEqual(Path(task['file_path']).read_bytes(), b'preserve')
                self.assertEqual(task['status'], 'failed')
            finally:
                store.executor.shutdown()

    def test_return_to_queue_runs_next_task_before_returned_task(self):
        started = []
        def worker(task, directory, progress, stop_event=None):
            started.append(task['sequence'])
            if task['sequence'] == 1 and len(started) == 1:
                self.assertTrue(stop_event.wait(2))
                raise module.downloader.DownloadPaused()
            path = Path(task['file_path'])
            path.write_bytes(b'test only')
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, parse_interval=0)
            store.settings(1)
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.tick()
                self.wait_until(lambda: started == [1])
                self.assertEqual(store.action('requeue', [store.tasks[0]['id']]), 1)
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'queued')
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(started, [1, 2])
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(started, [1, 2, 1])
            finally:
                store.executor.shutdown()

    def test_returned_task_keeps_queue_position_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.action('requeue', [store.tasks[0]['id']])
                restored = module.TaskStore(Path(folder) / 'state')
                try:
                    order = sorted(restored.tasks, key=lambda t:t['queue_order'])
                    self.assertEqual([t['sequence'] for t in order], [2, 1])
                finally:
                    restored.executor.shutdown()
            finally:
                store.executor.shutdown()

    def test_batches_share_limit_and_lowering_waits_for_running(self):
        gates = {n: threading.Event() for n in (1, 2, 3)}
        started = set()
        def worker(task, directory, progress, stop_event=None):
            number = task['sequence']
            started.add(number)
            gates[number].wait(2)
            path = Path(task['file_path'])
            path.write_bytes(b'test only')
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, parse_interval=0)
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.submit([self.item(3)], folder)
                store.tick()
                self.wait_until(lambda: started == {1, 2})
                store.settings(1)
                gates[1].set()
                self.wait_until(lambda: len(store.running) == 1)
                store.tick()
                self.assertEqual(started, {1, 2})
                gates[2].set()
                self.wait_until(lambda: not store.running)
                store.tick()
                self.wait_until(lambda: 3 in started)
                gates[3].set()
                self.wait_until(lambda: not store.running)
                self.assertTrue(all(t['status'] == 'downloaded' for t in store.tasks))
                restored = module.TaskStore(Path(folder) / 'state', worker)
                self.assertEqual(restored.parallel, 1)
                self.assertEqual(len(restored.tasks), 3)
                restored.executor.shutdown()
            finally:
                for gate in gates.values():
                    gate.set()
                store.executor.shutdown()

    def test_single_failure_does_not_stop_other_tasks(self):
        def worker(task, directory, progress, stop_event=None):
            if task['sequence'] == 1:
                raise RuntimeError('测试解析失败')
            path = Path(task['file_path'])
            path.write_bytes(b'test only')
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, parse_interval=0)
            store.settings(1)
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.submit([self.item(3)], folder)
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual([t['status'] for t in store.tasks], ['failed', 'queued', 'queued'])
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[1]['status'], 'downloaded')
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[2]['status'], 'downloaded')
            finally:
                store.executor.shutdown()

    def test_external_progress_occupies_a_global_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'legacy.log'
            log.write_text('[#a 1MiB/10MiB(10%) CN:4 DL:20KiB ETA:10s]\nFILE: /Downloads/001_甲_111.mp4.part\n', encoding='utf-8')
            store = module.TaskStore(Path(folder) / 'state')
            store.tasks.append({'id': 'legacy', 'video_id': '111', 'status': 'downloading',
                                'external_log': str(log), 'file_path': str(Path(folder) / 'old.mp4')})
            store.settings(1)
            store.submit([self.item(2)], folder)
            store.tick()
            self.assertEqual(store.tasks[1]['status'], 'queued')
            self.assertFalse(store.running)
            store.executor.shutdown()

    def test_duplicate_across_batches_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state')
            store.submit([self.item(1)], folder)
            with self.assertRaises(ValueError):
                store.submit([self.item(1)], folder)
            self.assertEqual(len(store.tasks), 1)
            store.executor.shutdown()

    def test_parser_starts_are_spaced_while_transfers_overlap(self):
        clock = [1000.0]
        gate = threading.Event()
        started = []
        def worker(task, directory, progress, stop_event=None):
            started.append((task['sequence'], clock[0]))
            gate.wait(2)
            path = Path(task['file_path'])
            path.write_bytes(b'test only')
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, clock=lambda: clock[0])
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.tick()
                self.wait_until(lambda: len(started) == 1)
                clock[0] += 30
                store.tick()
                self.assertEqual(len(started), 1)
                clock[0] += 1
                store.tick()
                self.wait_until(lambda: len(started) == 2)
                self.assertEqual(len(store.running), 2)
                self.assertEqual(started, [(1, 1000.0), (2, 1031.0)])
            finally:
                gate.set()
                store.executor.shutdown()

    def test_rate_limit_cools_down_then_retries_without_blocking_batch(self):
        clock = [1000.0]
        calls = []
        def worker(task, directory, progress, stop_event=None):
            calls.append(task['sequence'])
            if len(calls) == 1:
                raise module.downloader.RateLimitError('请求频繁', retry_after=65)
            path = Path(task['file_path'])
            path.write_bytes(b'test only')
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, clock=lambda: clock[0])
            try:
                store.submit([self.item(1), self.item(2)], folder)
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual([t['status'] for t in store.tasks], ['queued', 'queued'])
                self.assertEqual(store.tasks[0]['retry_at'], 1065.0)
                self.assertEqual(len(store.tasks[0]['retry_history']), 1)
                clock[0] = 1064
                store.tick()
                self.assertEqual(calls, [1])
                clock[0] = 1065
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'downloaded')
                clock[0] = 1096
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[1]['status'], 'downloaded')
                self.assertEqual(calls, [1, 1, 2])
            finally:
                store.executor.shutdown()

    def test_transient_timeout_requeues_same_task_and_preserves_partial(self):
        clock = [1000.0]
        calls = []
        def worker(task, directory, progress, stop_event=None):
            calls.append((task['id'], task.get('resume_requested', False)))
            partial = Path(task['file_path'] + '.part')
            if len(calls) == 1:
                partial.write_bytes(b'original bytes')
                raise TimeoutError('The read operation timed out')
            self.assertEqual(partial.read_bytes(), b'original bytes')
            path = Path(task['file_path'])
            path.write_bytes(partial.read_bytes())
            return path
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, clock=lambda: clock[0])
            try:
                store.submit([self.item(1)], folder)
                task_id = store.tasks[0]['id']
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'queued')
                self.assertEqual(store.tasks[0]['network_retries'], 1)
                self.assertEqual(store.tasks[0]['retry_at'], 1015.0)
                self.assertTrue(store.tasks[0]['resume_requested'])
                clock[0] = 1031.0
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'downloaded')
                self.assertEqual(calls, [(task_id, False), (task_id, True)])
                self.assertEqual(len(store.tasks), 1)
            finally:
                store.executor.shutdown()

    def test_http_404_does_not_auto_retry(self):
        def worker(task, directory, progress, stop_event=None):
            raise urllib.error.HTTPError(task['url'], 404, 'Not Found', {}, None)
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker)
            try:
                store.submit([self.item(1)], folder)
                store.tick()
                self.wait_until(lambda: not store.running)
                self.assertEqual(store.tasks[0]['status'], 'failed')
                self.assertNotIn('network_retries', store.tasks[0])
            finally:
                store.executor.shutdown()

    def test_transient_timeout_has_three_retry_limit(self):
        clock = [1000.0]
        calls = []
        def worker(task, directory, progress, stop_event=None):
            calls.append(task['id'])
            raise TimeoutError('timed out')
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, clock=lambda: clock[0])
            try:
                store.submit([self.item(1)], folder)
                for attempt in range(4):
                    store.tick()
                    self.wait_until(lambda: not store.running)
                    if attempt < 3:
                        self.assertEqual(store.tasks[0]['status'], 'queued')
                        clock[0] += 100
                self.assertEqual(store.tasks[0]['status'], 'failed')
                self.assertEqual(store.tasks[0]['network_retries'], 3)
                self.assertEqual(len(calls), 4)
            finally:
                store.executor.shutdown()

    def test_repeated_rate_limit_has_a_retry_ceiling(self):
        clock = [1000.0]
        def worker(task, directory, progress, stop_event=None):
            raise module.downloader.RateLimitError('请求频繁')
        with tempfile.TemporaryDirectory() as folder:
            store = module.TaskStore(Path(folder) / 'state', worker, clock=lambda: clock[0])
            try:
                store.submit([self.item(1), self.item(2)], folder)
                for _ in range(6):
                    store.tick()
                    self.wait_until(lambda: not store.running)
                    clock[0] += 100
                self.assertEqual(store.tasks[0]['status'], 'failed')
                self.assertEqual(store.tasks[0]['rate_retries'], 5)
                self.assertEqual(store.tasks[1]['status'], 'queued')
            finally:
                store.executor.shutdown()

    def test_parser_cooldown_survives_service_restart(self):
        clock = [1000.0]
        def worker(task, directory, progress, stop_event=None):
            raise module.downloader.RateLimitError('请求频繁', retry_after=90)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'state'
            store = module.TaskStore(directory, worker, clock=lambda: clock[0])
            store.submit([self.item(1)], folder)
            store.tick()
            self.wait_until(lambda: not store.running)
            store.executor.shutdown()
            restored = module.TaskStore(directory, worker, clock=lambda: clock[0])
            try:
                self.assertEqual(restored.next_parse_at, 1090)
                restored.tick()
                self.assertFalse(restored.running)
                self.assertEqual(restored.tasks[0]['status'], 'queued')
                self.assertEqual(restored.tasks[0]['rate_retries'], 1)
            finally:
                restored.executor.shutdown()


if __name__ == '__main__':
    unittest.main()
