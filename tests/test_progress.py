from __future__ import annotations

from kbimporter.progress import ProgressTracker, build_panel, format_duration


def test_tracker_file_lifecycle():
    t = ProgressTracker()
    t.begin(2)
    t.set_current("C:/kb/a.pdf")
    t.file_started("C:/kb/a.pdf")
    t.start_subtask("3/6", "paddle", "job1")
    t.update_pages(72, 100)
    t.finish_subtask("3/6")
    t.file_finished("C:/kb/a.pdf", True)
    snap = t.snapshot()
    assert snap["total_files"] == 2
    assert snap["done_files"] == 1
    assert snap["subtask_done"] == 1
    file = snap["files"][0]
    assert file["name"] == "a.pdf"
    assert file["provider"] == "paddle"
    assert file["subtask_current"] == 3
    assert file["subtask_total"] == 6
    assert file["pages_done"] == 72
    assert file["pages_total"] == 100


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
    assert "1/2" in panel
    t.file_finished("x.pdf", True)
    final = build_panel(t.snapshot(), final=True)
    assert "最终: 成功 1" in final


def test_format_duration():
    assert format_duration(65) == "01:05"
    assert format_duration(3661) == "01:01:01"
