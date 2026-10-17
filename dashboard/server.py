#!/usr/bin/env python3
"""Local, persistent task queue for douyin-downloader. Standard library only."""
import copy
import csv
import http.client
import importlib.util
import json
import os
import platform
import re
import subprocess
import threading
import time
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
ADDRESS = ("127.0.0.1", 9180)
ACTIVE = {"resolving", "downloading", "verifying", "pausing"}
spec = importlib.util.spec_from_file_location("douyin_downloader", ROOT / "scripts/download_douyin.py")
downloader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloader)


def process_exists(pid):
    if platform.system() == "Windows":
        # os.kill(pid, 0) can terminate a process on Windows.
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                                    capture_output=True, encoding="utf-8", errors="replace", check=True)
        except (OSError, subprocess.CalledProcessError):
            return True  # An unavailable probe cannot prove that the process exited.
        return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(result.stdout.splitlines()))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def transient_network_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (500, 502, 503, 504)
    if isinstance(exc, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, ConnectionError)) or "timed out" in str(exc.reason).lower()
    return False


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def save_parallel(value, path=STATE / "settings.json"):
    if type(value) is not int or value not in (1, 2, 3, 4):
        raise ValueError("同时下载数量必须为 1–4 的整数")
    atomic_json(path, {"parallel": value})


def parse_log(log):
    results, pending = {}, None
    for line in log.splitlines():
        progress = downloader.parse_aria2_progress(line)
        if progress:
            pending = progress
        elif line.startswith("FILE:") and pending:
            match = re.search(r"_(\d+)\.mp4(?:\.part)?$", line.strip())
            if match:
                results[match[1]] = pending
            pending = None
        else:
            match = re.search(r"\[\d+\]\[完成\].*_(\d+)\.mp4", line)
            if match:
                results[match[1]] = {"status": "downloaded", "percent": 100, "speed": "—", "eta": "—"}
    return results


def share_links(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请粘贴抖音分享链接或分享文案")
    links = []
    for raw in re.findall(r'https?://[^\s<>"，。；！？、（）【】《》\u3000]+', text):
        raw = raw.rstrip(".,;!?，。；！？、）)]}】》\"'：:")
        parsed = downloader.urlparse(raw)
        if parsed.hostname not in ("douyin.com", "www.douyin.com", "v.douyin.com") or parsed.port not in (None, 80, 443) or parsed.username:
            raise ValueError("只接受抖音作品链接，请移除其他网址")
        source_id = downloader.video_id_from_url(raw)
        if parsed.hostname == "v.douyin.com" and parsed.path.strip("/"):
            links.append("https://v.douyin.com/" + parsed.path.strip("/") + "/")
        elif source_id:
            links.append("https://www.douyin.com/video/" + source_id)
        else:
            raise ValueError("请使用作品分享链接，不支持主页或直播链接")
    if not links or len(links) > 2000:
        raise ValueError("每次请提交 1–2000 条作品分享链接")
    return links


class TaskStore:
    def __init__(self, directory=STATE, worker=None, clock=time.time, parse_interval=31):
        self.directory = Path(directory)
        self.path = self.directory / "tasks.json"
        self.lock = threading.RLock()
        self.worker = worker or downloader.download_one
        self.clock = clock
        self.parse_interval = parse_interval
        self.gate_path = self.directory / "parser_gate.json"
        self.next_parse_at = json.loads(self.gate_path.read_text(encoding="utf-8")).get("next_parse_at", 0) if self.gate_path.exists() else 0
        settings = self.directory / "settings.json"
        self.parallel = json.loads(settings.read_text(encoding="utf-8"))["parallel"] if settings.exists() else 2
        if type(self.parallel) is not int or self.parallel not in (1, 2, 3, 4):
            raise ValueError("settings.json 的并发设置无效")
        self.tasks = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        for position, task in enumerate(self.tasks):
            task.setdefault("queue_order", position)
            if task["status"] == "pausing":
                task.update(status=task.pop("after_stop", "paused"), resume_requested=True, error=None, updated_at=now())
            elif task["status"] in ACTIVE and not task.get("external_log"):
                task.update(status="interrupted", error="任务中心曾中断，请核对保留的文件后重新提交", updated_at=now())
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.last_queue_order = max((t["queue_order"] for t in self.tasks), default=-1)
        self.running = set()
        self.controls = {}
        self.stopped = threading.Event()
        self.clearing = False
        self.clear_error = None
        self.counter_path = self.directory / "sequence.json"
        self.last_sequence = max([t["sequence"] for t in self.tasks] + [json.loads(self.counter_path.read_text(encoding="utf-8")).get("last_sequence", 0) if self.counter_path.exists() else 0])

    def writable(self):
        if self.clearing:
            raise ValueError("正在停止并清空任务，请稍后再操作")

    def submit_shares(self, text, output_dir):
        links = share_links(text)
        if not isinstance(output_dir, str) or not Path(output_dir).expanduser().is_absolute():
            raise ValueError("保存目录必须为绝对路径")
        directory = str(Path(output_dir).expanduser().resolve())
        with self.lock:
            self.writable()
            known = {t["url"] for t in self.tasks} | {t.get("share_url") for t in self.tasks}
            ids = {t.get("video_id") for t in self.tasks if t.get("video_id")}
            records, batch = [], uuid.uuid4().hex
            for url in links:
                source_id = downloader.video_id_from_url(url)
                if url in known or source_id and source_id in ids:
                    continue
                known.add(url)
                if source_id:
                    ids.add(source_id)
                self.last_sequence += 1
                sequence = self.last_sequence
                records.append({"id": uuid.uuid4().hex, "batch_id": batch, "sequence": sequence,
                                "url": url, "share_url": url, "video_id": source_id or "",
                                "title": "待获取作品标题", "auto_title": True,
                                "file_path": str(Path(directory) / f"{sequence:03d}_待获取作品标题.mp4"),
                                "output_dir": directory, "status": "queued", "percent": 0,
                                "speed": "—", "eta": "—", "created_at": now(), "updated_at": now()})
            atomic_json(self.counter_path, {"last_sequence": self.last_sequence})
            for task in records:
                self.last_queue_order += 1
                task["queue_order"] = self.last_queue_order
            self.tasks.extend(records)
            self.save()
            return {"batch_id": batch, "added": len(records), "skipped": len(links) - len(records)}

    def preview_share(self, text):
        links = share_links(text)
        if len(links) != 1:
            raise ValueError("每次只解析一条作品链接")
        source = downloader.canonical_source(links[0])
        video_id = downloader.video_id_from_url(source)
        with self.lock:
            self.writable()
            if any(t.get("video_id") == video_id for t in self.tasks):
                return {"status": "duplicate", "url": source, "video_id": video_id}
            delay = max(0, self.next_parse_at - self.clock())
            if delay:
                return {"status": "wait", "retry_after": delay}
            self.set_parser_gate(self.clock() + self.parse_interval)
        try:
            _, _, title = downloader.resolve_video(source)
        except downloader.RateLimitError as exc:
            with self.lock:
                retry_at = self.clock() + exc.retry_after
                self.set_parser_gate(max(self.next_parse_at, retry_at))
            return {"status": "wait", "retry_after": max(0, retry_at - self.clock()), "error": str(exc)}
        if not title.strip():
            raise ValueError("未取得作品标题")
        return {"status": "ready", "url": source, "video_id": video_id, "title": title}

    def open_local(self, task_id):
        with self.lock:
            task = next((t for t in self.tasks if t["id"] == task_id), None)
            if task is None:
                raise ValueError("任务不存在")
            file_path = Path(task["file_path"])
            directory = Path(task["output_dir"])
        windows = platform.system() == "Windows"
        if file_path.is_file():
            command = ["explorer", "/select,", str(file_path)] if windows else ["open", "-R", str(file_path)]
            subprocess.run(command, check=True)
            return
        if not directory.is_dir():
            raise ValueError("保存目录尚不存在")
        subprocess.run(["explorer" if windows else "open", str(directory)], check=True)

    def clear(self, mode, confirmed=False):
        if mode not in ("completed", "all") or confirmed is not True:
            raise ValueError("请确认清理范围，媒体和断点文件将保留")
        with self.lock:
            self.writable()
            targets = [t for t in self.tasks if mode == "all" or t["status"] == "downloaded"]
            if mode == "all" and any(t.get("external_log") and t["status"] in ACTIVE for t in targets):
                raise ValueError("有旧版外部进程正在运行，请先停止该进程")
            if not targets:
                return {"removed": 0, "clearing": False}
            archive = self.directory / "task_history" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex + ".json")
            atomic_json(archive, {"at": now(), "mode": mode, "tasks": targets})
            atomic_json(self.counter_path, {"last_sequence": self.last_sequence})
            if mode == "completed":
                remaining = [t for t in self.tasks if t["status"] != "downloaded"]
                atomic_json(self.path, remaining)
                self.tasks = remaining
                return {"removed": len(targets), "clearing": False, "archive": str(archive)}
            self.action("pause")
            self.clear_archive = archive
            self.clear_error = None
            self.clearing = True
            return {"removed": 0, "clearing": True, "archive": str(archive)}

    def save(self):
        atomic_json(self.path, self.tasks)

    def settings(self, value):
        with self.lock:
            save_parallel(value, self.directory / "settings.json")
            self.parallel = value

    def action(self, action, task_ids=None):
        allowed = {"pause": ACTIVE | {"queued"}, "retry": {"failed", "interrupted"},
                   "resume": {"paused", "blocked"}, "requeue": ACTIVE | {"queued"}}
        if action not in allowed:
            raise ValueError("不支持的任务操作")
        with self.lock:
            self.writable()
            if task_ids is not None:
                if not isinstance(task_ids, list) or not task_ids or any(not isinstance(i, str) for i in task_ids):
                    raise ValueError("task_ids 必须为非空任务ID列表")
                if set(task_ids) - {t["id"] for t in self.tasks}:
                    raise ValueError("任务不存在")
            targets = [t for t in self.tasks if (task_ids is None or t["id"] in task_ids) and t["status"] in allowed[action]]
            if task_ids is not None and len(targets) != len(set(task_ids)):
                raise ValueError("当前任务状态不允许此操作")
            for task in targets:
                if task.get("external_log") and task["status"] in ACTIVE:
                    raise ValueError("旧版独立进程的任务不支持网页暂停")
                if action != "pause":
                    if Path(task["file_path"]).exists():
                        raise ValueError("目标成品已存在，请先核对；不会覆盖文件")
                    if any(t["id"] != task["id"] and t["file_path"].casefold() == task["file_path"].casefold()
                           and (t["status"] == "queued" or t["status"] in ACTIVE) for t in self.tasks):
                        raise ValueError("已有其他任务正在处理该文件")
            for task in targets:
                task.setdefault("action_history", []).append({"at": now(), "action": action,
                                                               "previous_status": task["status"], "error": task.get("error")})
                if action in ("pause", "requeue"):
                    destination = "queued" if action == "requeue" else "paused"
                    if action == "requeue":
                        self.last_queue_order += 1
                        task.update(queue_order=self.last_queue_order, resume_requested=True)
                    if task["id"] in self.running:
                        task["status"] = "pausing"
                        task["after_stop"] = destination
                        self.controls[task["id"]].set()
                    else:
                        task["status"] = destination
                    task.update(speed="—", eta="—", updated_at=now())
                else:
                    self.last_queue_order += 1
                    task.update(status="queued", queue_order=self.last_queue_order, resume_requested=True,
                                error=None, rate_retries=0, network_retries=0, updated_at=now())
                    task.pop("retry_at", None)
            if targets:
                self.save()
            return len(targets)

    def submit(self, items, output_dir):
        if not isinstance(items, list) or not items or len(items) > 2000:
            raise ValueError("items 必须为 1–2000 条作品")
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            raise ValueError("output_dir 必须为绝对路径")
        directory = directory.resolve()
        batch_id = uuid.uuid4().hex
        records = []
        for item in items:
            if not isinstance(item, dict) or type(item.get("sequence")) is not int or item["sequence"] < 1:
                raise ValueError("作品序号必须为正整数")
            title, url = item.get("title"), item.get("url")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
                raise ValueError("作品必须包含标题和链接")
            parsed = downloader.urlparse(url)
            video_id = downloader.video_id_from_url(url)
            if parsed.scheme not in ("http", "https") or parsed.hostname not in ("www.douyin.com", "douyin.com") or not video_id:
                raise ValueError("请使用带作品 ID 的抖音标准链接")
            if item.get("video_id") and str(item["video_id"]) != video_id:
                raise ValueError("作品 ID 与链接不一致")
            expected = item.get("expected_title")
            if expected is not None and not isinstance(expected, str):
                raise ValueError("expected_title 必须为文本")
            path = directory / downloader.output_template(item["sequence"], title, video_id)
            if path.exists() or path.with_suffix(".mp4.part").exists():
                raise ValueError(f"目标或未完成文件已存在，请先核对：{path}")
            records.append({"id": uuid.uuid4().hex, "batch_id": batch_id,
                            "sequence": item["sequence"], "title": title, "url": url,
                            "video_id": video_id, "expected_title": expected,
                            "file_path": str(path), "output_dir": str(directory),
                            "status": "queued", "percent": 0, "speed": "—", "eta": "—",
                            "created_at": now(), "updated_at": now()})
        with self.lock:
            self.writable()
            names = [t["file_path"].casefold() for t in records]
            reserved = {t["file_path"].casefold() for t in self.tasks if t["status"] == "queued" or t["status"] in ACTIVE}
            if len(set(names)) != len(names) or reserved.intersection(names):
                raise ValueError("输出文件名重复或已有任务正在处理该文件")
            self.tasks.extend(records)
            self.last_sequence = max(self.last_sequence, *(t["sequence"] for t in records))
            for task in records:
                self.last_queue_order += 1
                task["queue_order"] = self.last_queue_order
            self.save()
        return batch_id

    def refresh_external(self):
        # Sparse .part size is not transferred size; use actual engine progress.
        before = copy.deepcopy(self.tasks)
        cache = {}
        for task in self.tasks:
            log = task.get("external_log")
            if not log or task["status"] not in ACTIVE:
                continue
            if log not in cache:
                path = Path(log)
                cache[log] = (parse_log(path.read_text(encoding="utf-8", errors="replace")), path.stat().st_mtime) if path.exists() else ({}, 0)
            progress, modified = cache[log]
            result = progress.get(task["video_id"], {})
            if result.get("status") == "downloaded":
                if Path(task["file_path"]).is_file():
                    task.update(result, downloaded_bytes=Path(task["file_path"]).stat().st_size,
                                completed_at=now(), updated_at=now(), stale=False)
                else:
                    task.update(status="failed", error="日志标记完成，但目标文件不存在", updated_at=now())
            elif result:
                task.update(result, stale=time.time() - modified > 90, updated_at=datetime.fromtimestamp(modified, timezone.utc).isoformat())
            pid = task.get("external_pid")
            if task["status"] in ACTIVE and pid:
                if not process_exists(pid):
                    task.update(status="interrupted", error="旧下载进程已退出，未见完成记录；保留未完成文件", updated_at=now())
        return before != self.tasks

    def snapshot(self):
        with self.lock:
            if self.refresh_external():
                self.save()
            return {"service": "douyin-downloader", "parallel": self.parallel,
                    "clearing": self.clearing, "clear_error": self.clear_error,
                    "default_output_dir": str(Path.home() / "Downloads/抖音下载"),
                    "task_actions": ["pause", "retry", "resume", "requeue"],
                    "parse_interval_seconds": self.parse_interval, "next_parse_at": self.next_parse_at,
                    "tasks": copy.deepcopy(self.tasks), "updated_at": now()}

    def set_parser_gate(self, timestamp):
        self.next_parse_at = timestamp
        atomic_json(self.gate_path, {"next_parse_at": timestamp})

    def update(self, task, values):
        with self.lock:
            if task["status"] == "pausing" and values.get("status") in ACTIVE:
                values = {k: v for k, v in values.items() if k != "status"}
            task.update(values, updated_at=now())
            self.save()

    def finish_stop(self, task):
        with self.lock:
            self.update(task, {"status": task.pop("after_stop", "paused"), "speed": "—", "eta": "—", "error": None})

    def run_task(self, task):
        try:
            if task.get("auto_title"):
                source = downloader.canonical_source(task["url"])
                source_id = downloader.video_id_from_url(source)
                with self.lock:
                    if any(t["id"] != task["id"] and t.get("video_id") == source_id for t in self.tasks):
                        raise ValueError("此分享链接对应的作品已在任务中心，无需重复下载")
                    self.update(task, {"url": source, "video_id": source_id})
            path = self.worker(task, Path(task["output_dir"]), lambda values: self.update(task, values),
                               stop_event=self.controls.get(task["id"]))
            self.update(task, {"status": "downloaded", "percent": 100, "speed": "—", "eta": "—",
                               "downloaded_bytes": path.stat().st_size, "completed_at": now(),
                               "error": None, "retry_at": None})
        except downloader.DownloadPaused:
            self.finish_stop(task)
        except downloader.RateLimitError as exc:
            with self.lock:
                if self.controls.get(task["id"], threading.Event()).is_set():
                    self.finish_stop(task)
                    return
                retries = task.get("rate_retries", 0)
                retry_at = self.clock() + exc.retry_after
                self.set_parser_gate(max(self.next_parse_at, retry_at))
                task.setdefault("retry_history", []).append({"at": now(), "error": str(exc), "retry_at": retry_at})
                if retries < 5:
                    task.update(status="queued", rate_retries=retries + 1, retry_at=retry_at,
                                error=f"解析限流，冷却后自动重试（{retries + 1}/5）", updated_at=now())
                else:
                    task.update(status="failed", error="解析限流，已达5次自动重试上限；其他任务继续", updated_at=now())
                self.save()
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            with self.lock:
                paused = self.controls.get(task["id"], threading.Event()).is_set()
                if paused:
                    self.finish_stop(task)
                elif transient_network_error(exc) and task.get("network_retries", 0) < 3:
                    retries = task.get("network_retries", 0) + 1
                    retry_at = self.clock() + 15 * (2 ** (retries - 1))
                    task.setdefault("retry_history", []).append({"at": now(), "error": str(exc), "retry_at": retry_at})
                    task.update(status="queued", resume_requested=True, network_retries=retries,
                                retry_at=retry_at, error=f"网络异常，等待自动重试（{retries}/3）：{exc}", updated_at=now())
                    self.save()
                else:
                    task.update(status="failed", error=str(exc), updated_at=now())
                    self.save()
        finally:
            with self.lock:
                self.running.discard(task["id"])
                self.controls.pop(task["id"], None)

    def tick(self):
        with self.lock:
            if self.clearing:
                if not self.running:
                    try:
                        atomic_json(self.clear_archive, {"at": now(), "mode": "all", "tasks": self.tasks})
                        atomic_json(self.path, [])
                        self.tasks = []
                    except OSError as exc:
                        self.clear_error = "清空未完成，任务和媒体保留：" + str(exc)
                    self.clearing = False
                return
            changed = self.refresh_external()
            external_count = sum(bool(t.get("external_log")) and t["status"] in ACTIVE for t in self.tasks)
            slots = max(0, self.parallel - len(self.running) - external_count)
            timestamp = self.clock()
            eligible = sorted((t for t in self.tasks if t["status"] == "queued" and t["id"] not in self.running
                               and (t.get("retry_at") or 0) <= timestamp),
                              key=lambda t: t["queue_order"])
            # Parser capacity and media-transfer concurrency are separate limits.
            starts = min(slots, 1) if self.parse_interval else slots
            queued = eligible[:starts] if timestamp >= self.next_parse_at else []
            for task in queued:
                task.update(status="resolving", started_at=now(), updated_at=now())
                task.pop("retry_at", None)
                task["error"] = None
                self.running.add(task["id"])
                self.controls[task["id"]] = threading.Event()
            if queued:
                self.set_parser_gate(timestamp + self.parse_interval)
            if queued or changed:
                self.save()
            for task in queued:
                self.executor.submit(self.run_task, task)

    def loop(self):
        while not self.stopped.wait(1):
            self.tick()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        hosts = {"127.0.0.1:9180", "localhost:9180"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or origin in {"http://" + host for host in hosts})

    def do_GET(self):
        if not self.local_request():
            return self.respond(403, {"error": "仅接受本机请求"})
        if self.path == "/":
            return self.respond(200, (ROOT / "dashboard/index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/health":
            return self.respond(200, {"service": "douyin-downloader"})
        if self.path == "/api/status":
            return self.respond(200, self.server.store.snapshot())
        if self.path.startswith("/api/batch/"):
            batch = self.path.rsplit("/", 1)[1]
            tasks = [t for t in self.server.store.snapshot()["tasks"] if t["batch_id"] == batch]
            return self.respond(200 if tasks else 404, {"tasks": tasks})
        self.respond(404, {"error": "不存在的路径"})

    def do_POST(self):
        if not self.local_request() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(403, {"error": "仅接受本机 JSON 请求"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError("请求大小无效")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须为 JSON 对象")
            if self.path == "/api/settings":
                self.server.store.settings(body.get("parallel"))
                return self.respond(200, {"parallel": self.server.store.parallel})
            if self.path == "/api/tasks":
                if not isinstance(body.get("output_dir"), str):
                    raise ValueError("必须指定 output_dir")
                batch = self.server.store.submit(body.get("items"), body["output_dir"])
                return self.respond(201, {"batch_id": batch})
            if self.path == "/api/shares":
                result = self.server.store.submit_shares(body.get("text"), body.get("output_dir"))
                return self.respond(201, result)
            if self.path == "/api/preview":
                return self.respond(200, self.server.store.preview_share(body.get("url")))
            if self.path == "/api/open-local":
                self.server.store.open_local(body.get("task_id"))
                return self.respond(200, {"opened": True})
            if self.path == "/api/clear":
                return self.respond(200, self.server.store.clear(body.get("mode"), body.get("confirmed")))
            if self.path == "/api/actions":
                if body.get("scope") not in ("all", "tasks"):
                    raise ValueError("请明确操作全部任务或指定任务")
                if body["scope"] == "tasks" and "task_ids" not in body:
                    raise ValueError("必须指定task_ids")
                count = self.server.store.action(body.get("action"), None if body["scope"] == "all" else body["task_ids"])
                return self.respond(200, {"changed": count, "action": body["action"]})
            self.respond(404, {"error": "不存在的路径"})
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            self.respond(400, {"error": str(exc)})


def main():
    downloader.configure_utf8_output()
    # Bind first, so a racing second startup cannot alter persisted tasks.
    server = ThreadingHTTPServer(ADDRESS, Handler)
    server.store = TaskStore()
    threading.Thread(target=server.store.loop, daemon=True).start()
    print("douyin-downloader: http://127.0.0.1:9180", flush=True)
    try:
        server.serve_forever()
    finally:
        server.store.stopped.set()
        server.server_close()


if __name__ == "__main__":
    main()
