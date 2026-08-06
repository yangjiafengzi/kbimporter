from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

from kbimporter import convert
from kbimporter.config import Config


def test_find_files_skips_existing_md_and_skip_dirs(cfg: Config):
    scan = cfg.scan_dir
    d1 = scan / "田野调查笔记" / "p1" / "其他材料"
    d1.mkdir(parents=True)
    (d1 / "a.pdf").write_bytes(b"pdf")
    (d1 / "a.md").write_text("md", encoding="utf-8")  # 已有同名 md -> 跳过
    (d1 / "b.pdf").write_bytes(b"pdf2")  # 无同名 md -> 处理
    (d1 / "c.docx").write_bytes(b"docx")
    (scan / "0向量化").mkdir()
    (scan / "0向量化" / "skip.pdf").write_bytes(b"pdf3")
    pdfs, others = convert.find_files(scan, cfg)
    assert [p.name for p in pdfs] == ["b.pdf"]
    assert [p.name for p in others] == ["c.docx"]


def test_find_files_respects_skip_existing_md_false(cfg: Config):
    d = cfg.scan_dir / "项目文献" / "p"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"pdf")
    (d / "a.md").write_text("md", encoding="utf-8")
    cfg.skip_existing_md = False
    pdfs, _ = convert.find_files(cfg.scan_dir, cfg)
    assert [p.name for p in pdfs] == ["a.pdf"]


def test_run_convert_dry_run_reports_without_running(cfg: Config):
    d = cfg.scan_dir / "项目文献" / "p"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"pdf")
    (d / "b.docx").write_bytes(b"docx")
    stats = convert.run_convert(cfg, dry_run=True, scan_dir=cfg.scan_dir)
    assert stats["total"] == 2
    assert stats["pdf"] == 1
    assert stats["markitdown"] == 1
    assert (d / "a.pdf").exists() and (d / "b.docx").exists()
    assert not cfg.ocr_work_dir.exists()  # dry-run 不应创建任何工作目录


def test_process_markitdown_dry_run_plans_only(cfg: Config):
    d = cfg.scan_dir / "x"
    d.mkdir(parents=True)
    f = d / "a.docx"
    f.write_bytes(b"docx")
    failed: list[str] = []
    count = convert.process_markitdown(
        [f], cfg.scan_dir, cfg, dry_run=True, failed_files=failed,
        log=convert.logging.getLogger("t"),
    )
    assert count == 1
    assert not (d / "a.md").exists()


def test_process_markitdown_creates_output_dir(cfg: Config, monkeypatch):
    d = cfg.scan_dir / "x"
    d.mkdir(parents=True)
    f = d / "a.docx"
    f.write_bytes(b"docx")

    def fake_run(cmd, capture_output=True, text=True, timeout=120):
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_text("# md", encoding="utf-8")  # 不创建父目录，由被测代码保证
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(convert.subprocess, "run", fake_run)
    failed: list[str] = []
    count = convert.process_markitdown(
        [f], cfg.scan_dir, cfg, dry_run=False, failed_files=failed,
        log=convert.logging.getLogger("t"),
    )
    assert count == 1
    assert not failed
    assert (d / "a.md").read_text(encoding="utf-8") == "# md"


def test_run_mineru_single_returns_md(cfg: Config, monkeypatch):
    d = cfg.ocr_work_dir / "mineru_out" / "doc" / "auto"
    d.mkdir(parents=True)
    md = d / "doc.md"
    md.write_text("mineru", encoding="utf-8")
    monkeypatch.setattr(convert, "run_with_kill", lambda cmd, timeout: 0)
    pdf = cfg.scan_dir / "doc.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"pdf")
    result = convert._run_mineru_single(pdf, cfg, convert.logging.getLogger("t"))
    assert result == md


def test_retry_engines_mineru_fallback(cfg: Config, monkeypatch):
    d = cfg.project_root / "p"
    d.mkdir(parents=True)
    pdf = d / "doc.pdf"
    pdf.write_bytes(b"pdf")

    def fake_mineru(pdf_path, cfg_, log):
        md = cfg_.ocr_work_dir / "mineru_out" / "doc.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("mineru md", encoding="utf-8")
        return md

    monkeypatch.setattr(convert, "_run_mineru_single", fake_mineru)
    cfg.engines = ["mineru"]
    failed: list[str] = []
    success = convert.process_retry_engines(
        cfg.ocr_work_dir / "lost", cfg.scan_dir, cfg, dry_run=False,
        failed_files=failed, log=convert.logging.getLogger("t"),
        pending_pdfs=[pdf],
    )
    assert success == 1
    assert (d / "doc.md").read_text(encoding="utf-8") == "mineru md"
    assert not pdf.exists()
    assert any(p.name == "doc.pdf" for p in cfg.trash_dir.rglob("*.pdf"))
    conn = sqlite3.connect(cfg.state_db)
    row = conn.execute(
        "SELECT origin FROM file_origin WHERE file_path = ?",
        (str(d / "doc.md"),),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "ocr_md"


def test_retry_engines_marker_single_success(cfg: Config, monkeypatch):
    lost_dir = cfg.ocr_work_dir / "lost"
    lost_pdf = lost_dir / "项目文献" / "p" / "doc.pdf"
    lost_pdf.parent.mkdir(parents=True, exist_ok=True)
    lost_pdf.write_bytes(b"pdf")
    dest_dir = cfg.scan_dir / "项目文献" / "p"
    dest_dir.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, timeout):
        out_dir = Path(cmd[cmd.index("--output_dir") + 1])
        md = out_dir / "doc" / "doc.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# marker md", encoding="utf-8")
        return 0

    monkeypatch.setattr(convert, "run_with_kill", fake_run)
    cfg.engines = ["marker"]
    failed: list[str] = []
    success = convert.process_retry_engines(
        lost_dir, cfg.scan_dir, cfg, dry_run=False, failed_files=failed,
        log=convert.logging.getLogger("t"),
    )
    assert success == 1
    assert (dest_dir / "doc.md").read_text(encoding="utf-8") == "# marker md"
    assert not lost_pdf.exists()
    assert not list(lost_dir.rglob("retry_*"))


def test_retry_engines_cloud_fallback(cfg: Config, monkeypatch):
    d = cfg.project_root / "p"
    d.mkdir(parents=True)
    pdf = d / "doc.pdf"
    pdf.write_bytes(b"pdf")
    cfg.cloud_ocr.enabled = True
    cfg.engines = ["cloud"]

    def fake_cloud(cfg_, pdf_path, dest_md, dry_run=False, logger=None):
        Path(dest_md).write_text("cloud md", encoding="utf-8")
        return True

    monkeypatch.setattr(convert, "write_cloud_ocr_md", fake_cloud)
    success = convert.process_retry_engines(
        cfg.ocr_work_dir / "lost", cfg.scan_dir, cfg, dry_run=False,
        failed_files=[], log=convert.logging.getLogger("t"),
        pending_pdfs=[pdf],
    )
    assert success == 1
    assert (d / "doc.md").read_text(encoding="utf-8") == "cloud md"
    conn = sqlite3.connect(cfg.state_db)
    row = conn.execute(
        "SELECT origin FROM file_origin WHERE file_path = ?",
        (str(d / "doc.md"),),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "ocr_md"


def test_run_convert_engine_cloud_dry_run(cfg: Config):
    d = cfg.project_root / "p"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"pdf")
    stats = convert.run_convert(cfg, dry_run=True, scan_dir=cfg.scan_dir, engine="cloud")
    assert stats["pdf"] == 1


def test_run_with_kill_success():
    assert convert.run_with_kill([sys.executable, "-c", "print('ok')"], timeout=10) == 0


def test_run_with_kill_timeout():
    import subprocess as sp
    with pytest.raises(sp.TimeoutExpired):
        convert.run_with_kill(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=1
        )


def test_resolve_exe_uses_path(monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda name: r"C:\tools\marker.exe")
    assert convert._resolve_exe("marker", "ocr_env") == r"C:\tools\marker.exe"


def test_resolve_exe_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        "kbimporter.doctor._env_exe",
        lambda env, exe: (
            r"C:\fake\envs\ocr_env\Scripts\marker.exe" if env == "ocr_env" else None
        ),
    )
    assert convert._resolve_exe("marker", "ocr_env") == \
        r"C:\fake\envs\ocr_env\Scripts\marker.exe"
    assert convert._resolve_exe("ghost") == "ghost"


def test_build_marker_cmd_uses_resolved_exe(cfg, monkeypatch):
    monkeypatch.setattr(
        convert, "_resolve_exe", lambda cmd, *envs: r"C:\resolved\marker.exe"
    )
    cmd = convert.build_marker_cmd(Path("in"), Path("out"), cfg)
    assert cmd[0] == r"C:\resolved\marker.exe"


def _put_lost_pdf(cfg, rel="zotero文献库/library/doc.pdf"):
    lost_pdf = cfg.ocr_work_dir / "lost" / rel
    lost_pdf.parent.mkdir(parents=True, exist_ok=True)
    lost_pdf.write_bytes(b"pdf")
    dest_dir = cfg.scan_dir / Path(rel).parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    return lost_pdf


def test_run_convert_processes_lost_first(cfg, monkeypatch):
    lost_pdf = _put_lost_pdf(cfg)
    cfg.engines = ["mineru"]

    def fake_mineru(pdf_path, cfg_, log):
        md = cfg_.ocr_work_dir / "mineru_out" / "doc.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("mineru md", encoding="utf-8")
        return md

    monkeypatch.setattr(convert, "_run_mineru_single", fake_mineru)
    stats = convert.run_convert(cfg, dry_run=False, scan_dir=cfg.scan_dir)
    assert stats["pdf"] == 1
    assert stats["retry"] == 1
    assert (cfg.scan_dir / "zotero文献库" / "library" / "doc.md").read_text(
        encoding="utf-8"
    ) == "mineru md"
    assert not lost_pdf.exists()


def test_run_convert_skips_still_failed_in_stage3(cfg, monkeypatch):
    lost_pdf = _put_lost_pdf(cfg)
    cfg.engines = ["mineru"]
    calls = {"n": 0}

    def fake_mineru(pdf_path, cfg_, log):
        calls["n"] += 1
        return None

    monkeypatch.setattr(convert, "_run_mineru_single", fake_mineru)
    stats = convert.run_convert(cfg, dry_run=False, scan_dir=cfg.scan_dir)
    assert stats["failed"] == 1
    assert stats["pdf"] == 0
    assert calls["n"] == 1  # 阶段0 失败后，阶段3 不重复重试
    assert lost_pdf.exists()
