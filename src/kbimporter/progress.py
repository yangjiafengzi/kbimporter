from __future__ import annotations

import os
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field


@dataclass
class FileProgress:
    """单个文件的转换进度。"""
    name: str  # 展示名（文件基础名）
    status: str = "queued"  # queued / running / done / failed / skipped
    provider: str = ""
    subtask_current: int = 0
    subtask_total: int = 0
    subtask_completed: int = 0
    pages_completed: int = 0
    current_pages: int = 0
    pages_done: int = 0
    pages_total: int = 0
    job_id: str = ""
    retries: int = 0
    started_at: float = 0.0
    elapsed: float = 0.0
    updated_at: float = 0.0
    order: int = 0
    finished_tags: set[str] = field(default_factory=set)


class ProgressTracker:
    """线程安全的进度数据中心：各并发任务上报事件，渲染器只读快照。"""

    def __init__(self, max_alerts: int = 8):
        self._lock = threading.Lock()
        self._files: dict[str, FileProgress] = {}
        self._alerts: deque[str] = deque(maxlen=max_alerts)
        self._local = threading.local()
        self._next_order = 0
        self.total_files = 0
        self.done_files = 0
        self.failed_files = 0
        self.skipped_files = 0
        self.started_at = time.time()

    def reset(self):
        with self._lock:
            self._files.clear()
            self._alerts.clear()
            self.total_files = 0
            self.done_files = 0
            self.failed_files = 0
            self.skipped_files = 0
            self.started_at = time.time()
            self._local = threading.local()
            self._next_order = 0

    def begin(self, total_files: int):
        with self._lock:
            self.total_files = total_files

    # ---- 当前文件上下文（线程局部） ----
    def set_current(self, key: str | None):
        self._local.current = key

    def current(self) -> str | None:
        return getattr(self._local, "current", None)

    def clear_current(self):
        self._local.current = None

    # ---- 事件上报 ----
    def _file(self, key: str) -> FileProgress:
        fp = self._files.get(key)
        if fp is None:
            fp = FileProgress(name=os.path.basename(key) or key)
            fp.order = self._next_order
            self._next_order += 1
            self._files[key] = fp
        return fp

    def file_started(self, key: str):
        with self._lock:
            fp = self._file(key)
            fp.status = "running"
            fp.started_at = time.time()
            fp.updated_at = time.time()

    def file_queued(self, key: str):
        with self._lock:
            fp = self._file(key)
            fp.status = "queued"
            fp.updated_at = time.time()

    def file_finished(self, key: str, ok: bool):
        with self._lock:
            fp = self._file(key)
            fp.status = "done" if ok else "failed"
            fp.elapsed = time.time() - (fp.started_at or time.time())
            fp.updated_at = time.time()
            if ok:
                self.done_files += 1
            else:
                self.failed_files += 1

    def file_skipped(self, key: str):
        with self._lock:
            fp = self._file(key)
            fp.status = "skipped"
            fp.updated_at = time.time()
            self.skipped_files += 1

    def set_provider(self, key: str, provider: str):
        with self._lock:
            fp = self._file(key)
            fp.provider = provider
            fp.updated_at = time.time()

    def set_file_total(self, key: str, total: int):
        with self._lock:
            fp = self._file(key)
            fp.pages_total = total
            fp.updated_at = time.time()

    def start_subtask(self, tag: str | None, provider: str, job_id: str):
        key = self.current()
        if not key:
            return
        current = total = 0
        if tag and "/" in tag:
            try:
                cur_s, tot_s = tag.split("/", 1)
                current, total = int(cur_s), int(tot_s)
            except ValueError:
                pass
        if total <= 0:
            current, total = 1, 1
        with self._lock:
            fp = self._file(key)
            fp.status = "running"
            fp.provider = provider
            fp.job_id = job_id
            fp.subtask_current = current
            fp.subtask_total = total
            fp.updated_at = time.time()

    def update_pages(self, pages_done: int, pages_total: int | None = None):
        key = self.current()
        if not key:
            return
        with self._lock:
            fp = self._file(key)
            fp.current_pages = pages_done
            if pages_total is not None and fp.pages_total <= 0:
                fp.pages_total = pages_total
            fp.updated_at = time.time()

    def finish_subtask(self, tag: str | None, pages: int = 0):
        key = self.current()
        if not key:
            return
        with self._lock:
            fp = self._file(key)
            tag_key = tag or "1/1"
            if tag_key in fp.finished_tags:
                return  # 同一子任务重复完成（重试/断点）只计一次
            fp.finished_tags.add(tag_key)
            fp.subtask_completed += 1
            if fp.subtask_total <= 0:
                fp.subtask_total = 1
            fp.pages_completed += max(0, pages)
            fp.current_pages = 0
            fp.updated_at = time.time()

    def add_alert(self, message: str):
        with self._lock:
            self._alerts.append(message)

    def snapshot(self) -> dict:
        with self._lock:
            files = [
                {
                    "name": fp.name,
                    "status": fp.status,
                    "provider": fp.provider,
                    "subtask_current": fp.subtask_current,
                    "subtask_total": fp.subtask_total,
                    "subtask_completed": fp.subtask_completed,
                    "pages_completed": fp.pages_completed,
                    "current_pages": fp.current_pages,
                    "pages_total": fp.pages_total,
                    "job_id": fp.job_id,
                    "retries": fp.retries,
                    "elapsed": fp.elapsed,
                    "updated_at": fp.updated_at,
                    "order": fp.order,
                }
                for fp in self._files.values()
            ]
            subtask_total = sum(f["subtask_total"] for f in files)
            subtask_done = sum(f["subtask_completed"] for f in files)
            done = self.done_files + self.failed_files + self.skipped_files
            total_files = max(self.total_files, done)
            return {
                "total_files": total_files,
                "done_files": self.done_files,
                "failed_files": self.failed_files,
                "skipped_files": self.skipped_files,
                "subtask_done": subtask_done,
                "subtask_total": subtask_total,
                "files": files,
                "alerts": list(self._alerts),
                "elapsed": time.time() - self.started_at,
            }


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    return sum(_char_width(ch) for ch in text)


def _truncate_middle(text: str, max_width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断长文本，保留开头和结尾，中间用省略号连接。"""
    if _display_width(text) <= max_width:
        return text
    budget = max_width - _display_width(ellipsis)
    head_budget = budget * 3 // 5
    tail_budget = budget - head_budget
    head = ""
    for ch in text:
        w = _char_width(ch)
        if head_budget - w < 0:
            break
        head += ch
        head_budget -= w
    tail = ""
    for ch in reversed(text):
        w = _char_width(ch)
        if tail_budget - w < 0:
            break
        tail = ch + tail
        tail_budget -= w
    return head + ellipsis + tail


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def build_panel(snapshot: dict, final: bool = False, width: int = 96) -> str:
    """把进度快照渲染成终端面板文本（纯函数，便于测试）。"""
    lines = [
        "OCR 进度  "
        f"文件 {snapshot['done_files']}/{snapshot['total_files']}  "
        f"子任务 {snapshot['subtask_done']}/{snapshot['subtask_total']}  "
        f"耗时 {format_duration(snapshot['elapsed'])}",
        "-" * width,
        _pad_display("文件", 40)
        + _pad_display("状态", 12)
        + _pad_display("子任务", 8)
        + _pad_display("页", 12)
        + _pad_display("通道", 10),
    ]
    files = snapshot["files"]
    max_rows = 10
    hidden_above = 0
    if final:
        shown = files[:max_rows]
    else:
        # 按处理顺序排列，窗口锚定“最后一个正在识别的文件”：
        # 完成的文件自然往上滚出视野，正在识别的固定在下方，
        # 等待中的最多在底部露出 2 个。
        ordered = sorted(files, key=lambda f: f.get("order", 0))
        running_idx = [
            i for i, f in enumerate(ordered)
            if f["status"] == "running"
        ]
        if running_idx:
            anchor = running_idx[-1]
            queued_after = sum(
                1 for f in ordered[anchor + 1:] if f["status"] == "queued"
            )
            window_end = anchor + 1 + min(queued_after, 2)
        else:
            window_end = len(ordered)
        hidden_above = max(0, window_end - max_rows)
        shown = ordered[hidden_above:window_end]
    for fp in shown:
        name = _truncate_middle(fp["name"], 38)
        status = {
            "queued": "preparing",
            "running": "recognizing",
            "done": "done",
            "failed": "failed",
            "skipped": "skipped",
        }.get(fp["status"], fp["status"])
        subtask = (
            f"{fp['subtask_completed']}/{fp['subtask_total']}"
            if fp["subtask_total"] else "-"
        )
        pages_done = fp["pages_completed"] + fp["current_pages"]
        pages = (
            f"{pages_done}/{fp['pages_total']}"
            if fp["pages_total"] else "-"
        )
        lines.append(
            _pad_display(name, 40)
            + _pad_display(status, 12)
            + _pad_display(subtask, 8)
            + _pad_display(pages, 12)
            + _pad_display(fp["provider"], 10)
        )
    if hidden_above > 0:
        lines.append(f"... 上方还有 {hidden_above} 个文件")
    if snapshot["alerts"]:
        lines.append("-" * width)
        for alert in snapshot["alerts"][-3:]:
            lines.append(f"! {alert}")
    if final:
        lines.append("-" * width)
        lines.append(
            f"最终: 成功 {snapshot['done_files']}  失败 {snapshot['failed_files']}"
            f"  跳过 {snapshot['skipped_files']}"
        )
    return "\n".join(lines)


def _enable_ansi():
    """Windows 上启用 ANSI 转义（失败则静默，终端不支持时退化为普通输出）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class ProgressRenderer:
    """交互式进度面板：TTY 下定时重绘，非 TTY 下不输出控制字符。"""

    def __init__(self, tracker: ProgressTracker, stream=None, interval: float = 1.5):
        self.tracker = tracker
        self.stream = stream or sys.stdout
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        if self._enabled:
            _enable_ansi()

    def start(self):
        if not self._enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2)
            self._thread = None
            self._render(final=True)
        elif self._enabled:
            self._render(final=True)

    def _loop(self):
        while not self._stop.wait(self.interval):
            self._render()

    def _render(self, final: bool = False):
        text = build_panel(self.tracker.snapshot(), final=final)
        self.stream.write("\033[H\033[2J" + text + "\n")
        self.stream.flush()


tracker = ProgressTracker()
