#!/usr/bin/env python3
"""Download public Douyin videos using the user's working RCuts workflow."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

CONFIG_URL = "https://i.rcuts.com/update/247"
VIDEO_ID_RE = re.compile(r"(?:(?:/video/)|(?:/note/))(\d+)")
SAFE_CHARS_RE = re.compile(r"[^\w\u4e00-\u9fff.-]+", re.UNICODE)


class RateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: float = 65):
        super().__init__(message)
        self.retry_after = max(65, retry_after)


class DownloadPaused(Exception):
    pass


def video_id_from_url(url: str) -> str | None:
    match = VIDEO_ID_RE.search(urlparse(url).path)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(url).query)
    for key in ("modal_id", "aweme_id", "item_id"):
        value = query.get(key, [None])[0]
        if value and value.isdigit():
            return value
    return None


def output_template(sequence: int, title: str, video_id: str) -> str:
    if not video_id or not video_id.isdigit():
        raise ValueError("抖音作品 ID 必须是纯数字")
    slug = SAFE_CHARS_RE.sub("_", title).strip("._-")
    slug = re.sub(r"_+", "_", slug)[:90] or "未命名作品"
    slug = slug.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    return f"{sequence:03d}_{slug}_{video_id}.mp4"


def canonical_source(url: str) -> str:
    def validate(value, allow_share_redirect=False):
        parsed = urlparse(value)
        douyin_host = parsed.hostname in ("douyin.com", "www.douyin.com", "v.douyin.com")
        share_redirect = (allow_share_redirect and parsed.scheme == "https" and
                          parsed.hostname == "www.iesdouyin.com" and
                          re.fullmatch(r"/share/video/\d+/?", parsed.path) is not None)
        if parsed.scheme not in ("http", "https") or not (douyin_host or share_redirect) or parsed.port not in (None, 80, 443) or parsed.username:
            raise ValueError("分享链接只能跳转到抖音网站")
    validate(url)
    source_id = video_id_from_url(url)
    if not source_id:
        class VideoIdRedirect(Exception):
            def __init__(self, video_id):
                self.video_id = video_id

        class DouyinRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                validate(newurl, allow_share_redirect=True)
                redirected_id = video_id_from_url(newurl)
                if redirected_id:
                    if fp is not None:
                        fp.close()
                    raise VideoIdRedirect(redirected_id)
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.build_opener(DouyinRedirect()).open(request, timeout=30) as response:
                validate(response.url, allow_share_redirect=True)
                source_id = video_id_from_url(response.url)
        except VideoIdRedirect as redirect:
            source_id = redirect.video_id
    if not source_id:
        raise ValueError("分享链接未取得作品 ID，请粘贴该作品的标准链接")
    return "https://www.douyin.com/video/" + source_id


def load_manifest(path: Path) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"sequence", "title", "url"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV 必须包含 sequence,title,url 三列")
        for row in reader:
            url = (row.get("url") or "").strip()
            title = (row.get("title") or "").strip()
            sequence = int((row.get("sequence") or "").strip())
            url_id = video_id_from_url(url)
            provided_id = (row.get("douyin_id") or "").strip()
            if provided_id and url_id and provided_id != url_id:
                raise ValueError(f"第 {sequence} 条 douyin_id 与 URL 中的作品 ID 不一致")
            video_id = provided_id or url_id
            if not url or not video_id:
                raise ValueError(f"第 {sequence} 条缺少有效 URL 或作品 ID")
            rows.append({"sequence": sequence, "title": title or "未命名作品", "url": url, "video_id": video_id, "expected_title": (row.get("expected_title") or title).strip()})
    return rows


def parse_video_response(data: dict) -> tuple[str, str]:
    if str(data.get("code")) != "0":
        message = f"解析失败：{data.get('msg', '服务未返回成功状态')}"
        if any(word in message.lower() for word in ("请求频繁", "限流", "rate limit", "too many requests")):
            raise RateLimitError(message)
        raise RuntimeError(message)
    media_url = data.get("video_url", "")
    if not isinstance(media_url, str) or urlparse(media_url).scheme not in ("http", "https"):
        raise RuntimeError("解析服务没有返回有效的视频下载地址")
    return media_url, str(data.get("video_name", ""))


def verify_source_title(expected: str, actual: str) -> None:
    def normalize(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.split("#", 1)[0]).casefold()
    expected, actual = normalize(expected), normalize(actual)
    if not expected or not actual.startswith(expected):
        raise RuntimeError("解析标题不匹配原作品标题，停止下载并核对来源")


def resolve_video(source_url: str) -> tuple[str, str, str]:
    with urllib.request.urlopen(CONFIG_URL, timeout=30) as response:
        config = json.load(response)
    body = urlencode({"url": source_url, "token": config.get("token", ""), "clipboard": source_url}).encode()
    user_agent = config["ag"]
    request = urllib.request.Request(config["api"], data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": user_agent,
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            media_url, parsed_title = parse_video_response(json.load(response))
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        retry_after = exc.headers.get("Retry-After", "65")
        exc.close()
        raise RateLimitError("解析服务返回 HTTP 429，请求频繁", float(retry_after) if retry_after.isdigit() else 65) from exc
    return media_url, user_agent, parsed_title


def parse_aria2_progress(line: str) -> dict | None:
    match = re.search(r"\[#[^ ]+ ([^/\s]+)/([^\s(]+)\((\d+)%\).*?DL:([^\s\]]+)(?: ETA:([^\s\]]+))?", line)
    if not match:
        return None
    done, total, percent, speed, eta = match.groups()
    return {"percent": min(99, int(percent)), "transferred": done, "total_display": total,
            "speed": speed + "/s", "eta": eta or "—", "status": "downloading"}


def find_aria2(platform_name: str | None = None) -> str | None:
    # Windows has no safe SIGINT-based sidecar handoff in this process model.
    platform_name = platform_name or sys.platform
    if platform_name == "win32":
        return None
    engine = shutil.which("aria2c")
    if not engine and platform_name == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            for motrix_engine in sorted(applications.glob("*Motrix*.app/Contents/Resources/engine/aria2c")):
                if motrix_engine.is_file():
                    return str(motrix_engine)
    return engine


def download_one(item: dict[str, str | int], output_dir: Path, progress=None, stop_event=None) -> Path:

    def report(**values):
        if progress:
            progress(values)

    def check_pause():
        if stop_event is not None and stop_event.is_set():
            raise DownloadPaused()

    sequence = int(item["sequence"])
    title = str(item["title"])
    url = str(item["url"])
    expected_id = str(item["video_id"])
    output_dir.mkdir(parents=True, exist_ok=True)
    if not item.get("auto_title"):
        path = output_dir / output_template(sequence, title, expected_id)
        partial = path.with_suffix(".mp4.part")
        resume = bool(item.get("resume_requested")) and partial.exists()
        if path.exists() or (partial.exists() and not resume):
            raise FileExistsError(f"目标或未完成文件已存在，请先核对：{path}")
    check_pause()
    report(status="resolving")
    print(f"[{sequence:03d}][解析] 使用原快捷指令的 RCuts 接口", flush=True)
    media_url, user_agent, parsed_title = resolve_video(url)
    check_pause()
    print(f"[{sequence:03d}][解析成功] {parsed_title}", flush=True)
    if item.get("expected_title"):
        verify_source_title(str(item["expected_title"]), parsed_title)
    if item.get("auto_title"):
        title = parsed_title.split("#", 1)[0].strip()
        if not title:
            raise ValueError("未取得作品标题，停止下载；请核对分享链接")
        title = title[:48]
        path = output_dir / output_template(sequence, title, expected_id)
        partial = path.with_suffix(".mp4.part")
        if path.exists() or partial.exists():
            raise FileExistsError(f"目标或未完成文件已存在，请先核对：{path}")
        resume = False
        report(title=title, expected_title=parsed_title, file_path=str(path), auto_title=False)
    digest = hashlib.sha256()
    engine = find_aria2()
    partial_size = partial.stat().st_size if resume else 0
    probe_length = min(65536, partial_size) if partial_size >= 12 else 12
    probe = urllib.request.Request(media_url, headers={"User-Agent": user_agent, "Range": f"bytes=0-{probe_length - 1}"})
    with urllib.request.urlopen(probe, timeout=30) as response:
        prefix = response.read(probe_length)
        if prefix[4:8] != b"ftyp":
            raise RuntimeError("下载响应不是 MP4 文件")
        content_range = response.headers.get("Content-Range", "")
        total = int(content_range.rsplit("/", 1)[1]) if "/" in content_range else int(response.headers.get("Content-Length", "0"))
    if resume:
        with partial.open("rb") as file:
            if (partial_size and file.read(probe_length) != prefix) or (item.get("total_bytes") and total != item["total_bytes"]):
                raise RuntimeError("续传来源与原文件不一致，保留未完成文件并停止该条")
        if item.get("download_engine") == "urllib":
            engine = None
        elif engine and not Path(str(partial) + ".aria2").exists() and not item.get("transfer_complete"):
            raise RuntimeError("缺少aria2断点文件，无法安全续传；已保留原文件")
        if not engine and item.get("download_engine") != "urllib":
            raise RuntimeError("原aria2下载引擎不可用，保留断点文件")
    check_pause()
    report(status="downloading", total_bytes=total, download_engine="aria2" if engine else "urllib")
    if engine and not (resume and item.get("transfer_complete")):
        print(f"[{sequence:03d}][下载] 使用已安装的 aria2 四连接下载", flush=True)
        process = subprocess.Popen([
            engine, "--no-conf=true", "--split=4", "--max-connection-per-server=4",
            "--min-split-size=1M", "--auto-file-renaming=false", "--allow-overwrite=false",
            "--check-certificate=true", "--summary-interval=2", "--show-console-readout=false", "--console-log-level=warn",
            "--download-result=hide", "--file-allocation=none",
            f"--continue={'true' if resume else 'false'}",
            f"--dir={partial.parent}", f"--out={partial.name}", f"--user-agent={user_agent}", media_url,
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        finished = threading.Event()

        def stop_on_pause():
            while not finished.wait(.1):
                if stop_event is not None and stop_event.is_set():
                    try:
                        process.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    return

        if stop_event is not None:
            threading.Thread(target=stop_on_pause, daemon=True).start()
        try:
            for line in process.stdout:
                parsed = parse_aria2_progress(line)
                if parsed:
                    report(**parsed)
                    print(f"[{sequence:03d}] {line.strip()}", flush=True)
            result = process.wait()
            if result == 0:
                report(transfer_complete=True)
            check_pause()
            if result:
                raise RuntimeError(f"aria2 下载失败（退出码{result}），保留 .part 文件")
        finally:
            finished.set()
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()
    elif not engine and not (resume and item.get("transfer_complete")):
        if resume and item.get("download_engine") != "urllib":
            raise RuntimeError("原aria2下载引擎不可用，保留断点文件")
        offset = partial.stat().st_size if resume else 0
        headers = {"User-Agent": user_agent}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(media_url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            if offset and (response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                raise RuntimeError("服务器不支持所需续传范围，保留原文件")
            chunk = response.read(256 * 1024)
            if not offset and (len(chunk) < 12 or chunk[4:8] != b"ftyp"):
                raise RuntimeError("下载响应不是 MP4 文件，未保存为视频")
            downloaded = offset
            started = time.monotonic()
            last_progress = time.monotonic()
            with partial.open("ab" if offset else "xb") as file:
                while chunk:
                    check_pause()
                    file.write(chunk)
                    downloaded += len(chunk)
                    if time.monotonic() - last_progress >= 2:
                        print(f"[{sequence:03d}][下载] {downloaded / 1048576:.2f} / {total / 1048576:.2f} MiB", flush=True)
                        rate = (downloaded - offset) / max(time.monotonic() - started, .001)
                        report(percent=min(99, int(downloaded * 100 / total)) if total else None,
                               downloaded_bytes=downloaded, speed=f"{rate / 1024:.0f} KiB/s",
                               eta=f"{(total - downloaded) / rate:.0f}s" if total else "—")
                        last_progress = time.monotonic()
                    chunk = response.read(256 * 1024)
        if total and downloaded != total:
            raise RuntimeError(f"下载不完整：期望 {total} bytes，实际 {downloaded} bytes；保留 .part 文件")
        report(transfer_complete=True)
    check_pause()
    if total and partial.stat().st_size != total:
        raise RuntimeError("下载大小与服务器声明不一致，保留 .part 文件")
    report(status="verifying", percent=99, speed="—", eta="—")
    with partial.open("rb") as file:
        if file.read(12)[4:8] != b"ftyp":
            raise RuntimeError("下载响应不是 MP4 文件，保留 .part 文件供排查")
        file.seek(0)
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            check_pause()
            digest.update(chunk)
    check_pause()
    os.link(partial, path)
    partial.unlink()
    print(f"[{sequence:03d}][SHA256] {digest.hexdigest()}", flush=True)
    report(sha256=digest.hexdigest(), downloaded_bytes=path.stat().st_size)
    return path


def download_batch(items: list[dict], output_dir: Path, jobs: int = 2) -> list[Path]:
    if jobs not in (1, 2, 3, 4):
        raise ValueError("同时下载的视频数须为 1–4")
    names = [output_template(int(item["sequence"]), str(item["title"]), str(item["video_id"])).casefold() for item in items]
    if len(names) != len(set(names)):
        raise ValueError("批次包含重复输出文件名，停止以避免并发覆盖")

    def run(position: int, item: dict) -> Path:
        start = time.monotonic()
        print(f"[{position}/{len(items)}][{int(item['sequence']):03d}] 开始：{item['title']}", flush=True)
        path = download_one(item, output_dir)
        print(f"[{int(item['sequence']):03d}][完成] {path} ({path.stat().st_size} bytes, {time.monotonic() - start:.2f} 秒)", flush=True)
        return path

    completed: list[Path] = []
    failure = None
    cursor = 0
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        running = {}
        while running or (cursor < len(items) and failure is None):
            while failure is None and len(running) < jobs and cursor < len(items):
                item = items[cursor]
                running[executor.submit(run, cursor + 1, item)] = item
                cursor += 1
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                item = running.pop(future)
                try:
                    completed.append(future.result())
                except Exception as exc:
                    print(f"[{int(item['sequence']):03d}][失败] {exc}", file=sys.stderr, flush=True)
                    if failure is None:
                        failure = RuntimeError(f"第 {item['sequence']} 条失败，停止启动新任务；已启动的任务完成后保留结果")
    if failure:
        raise failure
    return completed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="复用原快捷指令的 RCuts 解析流程，直接下载抖音 MP4。仅需 Python 标准库。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="单条抖音视频链接")
    source.add_argument("--manifest", type=Path, help="CSV 批次清单，列为 sequence,title,url，可选 douyin_id")
    source.add_argument("--share-text", help="一条或多条分享链接/文案；自动编号，标题和地址在任务内解析")
    parser.add_argument("--sequence", type=int, default=1, help="单条作品序号，默认 1")
    parser.add_argument("--title", default="抖音作品", help="单条作品标题")
    parser.add_argument("--expected-title", help="原作品标题，用于阻断解析错作品；--title 可保留简短文件标题")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--jobs", type=int, choices=(1, 2, 3, 4), help="设置 Skill 全局同时下载数；省略则沿用网页设置，初始为 2")
    return parser.parse_args()


def dashboard_request(route: str, body=None):
    request = urllib.request.Request("http://127.0.0.1:9180" + route,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    # Local control traffic must not go through an environment HTTP proxy.
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read(65536))
            detail = data.get("error") if isinstance(data, dict) else None
        except ValueError:
            detail = None
        finally:
            exc.close()
        raise RuntimeError(f"任务中心 HTTP {exc.code}：{detail or exc.reason}") from exc


def dashboard_process_options(platform_name: str | None = None) -> dict:
    platform_name = platform_name or sys.platform
    if platform_name == "win32":
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0x00000008) |
                                  getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))}
    return {"start_new_session": True}


def ensure_dashboard() -> None:
    try:
        health = dashboard_request("/api/health")
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ConnectionRefusedError):
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise RuntimeError("9180 端口响应异常，停止启动任务中心，请核对已有服务") from exc
        root = Path(__file__).resolve().parents[1]
        state = root / "state"
        state.mkdir(exist_ok=True)
        with (state / "server.log").open("a") as log:
            process = subprocess.Popen([sys.executable, str(root / "dashboard/server.py")],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                                       **dashboard_process_options())
        for _ in range(40):
            try:
                health = dashboard_request("/api/health")
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError(f"任务中心启动失败，请检查 {state / 'server.log'}")
                time.sleep(.1)
        else:
            raise RuntimeError("任务中心启动超时")
    if health.get("service") != "douyin-downloader":
        raise RuntimeError("9180 端口被其他服务占用，停止提交任务")


def wait_for_batch(batch_id: str) -> None:
    previous = {}
    while True:
        tasks = dashboard_request("/api/batch/" + batch_id)["tasks"]
        for task in tasks:
            state = (task["status"], task.get("percent"))
            if previous.get(task["id"]) != state:
                print(f"[{task['sequence']:03d}][{task['status']}] {task.get('percent', 0)}% {task.get('error', '')}", flush=True)
                previous[task["id"]] = state
        if all(t["status"] in ("downloaded", "failed", "blocked", "interrupted") for t in tasks):
            if any(t["status"] != "downloaded" for t in tasks):
                raise RuntimeError("批次未全部完成，请在任务中心查看原因；已有文件保留")
            return
        time.sleep(2)


def managed_batch(items: list[dict], output_dir: Path, jobs=None) -> None:
    ensure_dashboard()
    if jobs is not None:
        dashboard_request("/api/settings", {"parallel": jobs})
    batch = dashboard_request("/api/tasks", {"items": items, "output_dir": str(output_dir.expanduser().resolve())})
    print(f"[任务中心] http://127.0.0.1:9180 · 批次 {batch['batch_id']}", flush=True)
    wait_for_batch(batch["batch_id"])


def managed_shares(text: str, output_dir: Path, jobs=None) -> None:
    ensure_dashboard()
    if jobs is not None:
        dashboard_request("/api/settings", {"parallel": jobs})
    batch = dashboard_request("/api/shares", {"text": text, "output_dir": str(output_dir.expanduser().resolve())})
    print(f"[任务中心] 已加入 {batch['added']} 条，跳过 {batch['skipped']} 条重复链接 · http://127.0.0.1:9180", flush=True)
    if batch["added"]:
        wait_for_batch(batch["batch_id"])


def configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_utf8_output()
    args = parse_args()
    try:
        if args.share_text is not None:
            managed_shares(args.share_text, args.output_dir, jobs=args.jobs)
            return 0
        elif args.manifest:
            items = load_manifest(args.manifest)
        else:
            video_id = video_id_from_url(args.url)
            if not video_id:
                raise ValueError("URL 中未找到数字作品 ID；分享链接或文案请用 --share-text，固定序号请使用标准作品链接")
            items = [{"sequence": args.sequence, "title": args.title, "url": args.url, "video_id": video_id, "expected_title": args.expected_title}]
        managed_batch(items, args.output_dir, jobs=args.jobs)
    except Exception as exc:
        print(f"[失败] {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
