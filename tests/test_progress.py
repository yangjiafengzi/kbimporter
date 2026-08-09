from __future__ import annotations

from kbimporter.progress import (
    ProgressTracker,
    _display_width,
    _pad_display,
    _truncate_middle,
    build_panel,
    format_duration,
)


def test_tracker_file_lifecycle():
    t = ProgressTracker()
    t.begin(2)
    t.set_current("C:/kb/a.pdf")
    t.set_file_total("C:/kb/a.pdf", 100)
    t.file_started("C:/kb/a.pdf")
    t.start_subtask("3/6", "paddle", "job1")
    t.update_pages(72, 100)
    t.finish_subtask("3/6", pages=28)
    t.file_finished("C:/kb/a.pdf", True)
    snap = t.snapshot()
    assert snap["total_files"] == 2
    assert snap["done_files"] == 1
    assert snap["subtask_done"] == 1
    file = snap["files"][0]
    assert file["name"] == "a.pdf"
    assert file["provider"] == "paddle"
    assert file["subtask_completed"] == 1
    assert file["subtask_total"] == 6
    assert file["pages_completed"] == 28
    assert file["current_pages"] == 0
    assert file["pages_total"] == 100


def test_finish_subtask_ignores_duplicate_tag():
    t = ProgressTracker()
    t.set_current("x.pdf")
    t.file_started("x.pdf")
    t.start_subtask("1/2", "mineru", "b1")
    t.finish_subtask("1/2", pages=10)
    t.finish_subtask("1/2", pages=10)  # 重复完成只计一次
    snap = t.snapshot()
    assert snap["subtask_done"] == 1
    assert snap["files"][0]["subtask_completed"] == 1
    assert snap["files"][0]["pages_completed"] == 10


def test_build_panel_uses_english_states():
    t = ProgressTracker()
    t.set_current("x.pdf")
    t.file_queued("x.pdf")
    assert "preparing" in build_panel(t.snapshot())
    t.file_started("x.pdf")
    assert "recognizing" in build_panel(t.snapshot())
    t.file_finished("x.pdf", True)
    assert "done" in build_panel(t.snapshot())


def test_tracker_alert_and_reset():
    t = ProgressTracker()
    t.add_alert("额度用完")
    assert t.snapshot()["alerts"] == ["额度用完"]
    t.reset()
    assert t.snapshot()["alerts"] == []


def test_build_panel_contains_progress_and_final():
    t = ProgressTracker()
    t.begin(1)
    t.set_current("x.pdf")
    t.file_started("x.pdf")
    t.start_subtask("1/2", "mineru", "b1")
    t.update_pages(5, 10)
    panel = build_panel(t.snapshot())
    assert "OCR 进度" in panel
    assert "x.pdf" in panel
    assert "mineru" in panel
    assert "0/2" in panel  # 已完成的子任务数/总子任务数
    t.finish_subtask("1/2", pages=5)
    assert "1/2" in build_panel(t.snapshot())
    t.file_finished("x.pdf", True)
    final = build_panel(t.snapshot(), final=True)
    assert "最终: 成功 1" in final


def test_format_duration():
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "01:01:01"


def test_long_filename_truncated_with_ellipsis():
    name = "中央编译局-2019-马克思恩格斯全集第38卷2版.pdf"
    short = _truncate_middle(name, 38)
    assert "…" in short
    assert _display_width(short) <= 38
    assert short.startswith("中央编译局")
    assert short.endswith(".pdf")


def test_panel_rows_align_by_display_width():
    t = ProgressTracker()
    t.set_current("x.pdf")
    t.file_started("x.pdf")
    t.start_subtask("1/2", "mineru", "b1")
    panel = build_panel(t.snapshot())
    row = panel.splitlines()[3]
    # 每列都按显示宽度补齐：文件40 + 状态12 + 子任务8 + 页12 + 通道10
    assert _display_width(row) == 82
    assert _pad_display("中央", 4) == "中央"
    assert _pad_display("ab", 4) == "ab  "


def _fp(name: str, status: str, updated: float, order: int = 0) -> dict:
    return {
        "name": name,
        "status": status,
        "provider": "mineru",
        "subtask_completed": 0,
        "subtask_total": 1,
        "pages_completed": 0,
        "current_pages": 0,
        "pages_total": 100,
        "job_id": "",
        "retries": 0,
        "elapsed": 0,
        "updated_at": updated,
        "order": order,
    }


def _snap(files, elapsed=10.0) -> dict:
    return {
        "total_files": len(files),
        "done_files": sum(1 for f in files if f["status"] == "done"),
        "failed_files": 0,
        "skipped_files": 0,
        "subtask_done": 0,
        "subtask_total": 0,
        "files": files,
        "alerts": [],
        "elapsed": elapsed,
    }


def test_panel_shows_active_in_queue_position():
    files = [_fp(f"done{i}.pdf", "done", 100 + i, order=i) for i in range(8)]
    files.append(_fp("running-a.pdf", "running", 200, order=8))
    files.append(_fp("queued-b.pdf", "queued", 201, order=9))
    panel = build_panel(_snap(files))
    assert "running-a.pdf" in panel
    assert "queued-b.pdf" in panel
    # 完成的在识别中的上方（按处理顺序排列）
    assert panel.index("done0.pdf") < panel.index("running-a.pdf")


def test_panel_window_follows_running_tail():
    files = [_fp(f"done{i}.pdf", "done", 100 + i, order=i) for i in range(8)]
    files += [
        _fp(f"run{8 + i}.pdf", "running", 1000 + i, order=8 + i)
        for i in range(4)
    ]
    panel = build_panel(_snap(files))
    assert "run11.pdf" in panel
    assert "done0.pdf" not in panel  # 顶部已完成的滚出视野
    assert "done1.pdf" not in panel
    assert "... 上方还有 2 个文件" in panel


def test_panel_shows_few_queued_below_running():
    files = [_fp(f"done{i}.pdf", "done", 100 + i, order=i) for i in range(8)]
    files += [
        _fp(f"run{8 + i}.pdf", "running", 1000 + i, order=8 + i)
        for i in range(4)
    ]
    files += [
        _fp(f"queued{12 + i}.pdf", "queued", 2000 + i, order=12 + i)
        for i in range(3)
    ]
    panel = build_panel(_snap(files))
    assert "run11.pdf" in panel
    assert "queued12.pdf" in panel
    assert "queued13.pdf" in panel
    assert "queued14.pdf" not in panel  # 等待中的最多露出 2 个
