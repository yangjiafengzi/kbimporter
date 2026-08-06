from __future__ import annotations

import sqlite3
from pathlib import Path

from kbimporter import dedupe
from kbimporter.config import Config


def _pdf(path: Path, content: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_hash_dedupe_moves_duplicate_to_trash(cfg: Config):
    proj = cfg.project_root / "村干部类型"
    proj.mkdir(parents=True)
    a = _pdf(proj / "a.pdf", b"same")
    b = _pdf(proj / "b.pdf", b"same")
    c = _pdf(proj / "c.pdf", b"other")
    count = dedupe.remove_duplicates_by_hash(
        proj, {".pdf"}, cfg, dry_run=False, logger=dedupe.logging.getLogger("t")
    )
    assert count == 1
    assert a.exists() and c.exists()
    assert not b.exists()
    assert any(p.name == "b.pdf" for p in cfg.trash_dir.rglob("*.pdf"))


def test_version_dedupe_keeps_original(cfg: Config, monkeypatch):
    proj = cfg.project_root / "p"
    proj.mkdir(parents=True)
    orig = _pdf(proj / "a.pdf", b"english")
    trans = _pdf(proj / "a-1.pdf", b"chinese")
    monkeypatch.setattr(
        dedupe, "extract_text",
        lambda fp: "english text" if Path(fp).name == "a.pdf" else "中文译本内容",
    )
    count = dedupe.process_duplicates(
        proj, {".pdf"}, cfg, dry_run=False, logger=dedupe.logging.getLogger("t")
    )
    assert count >= 1
    assert orig.exists()
    assert not trans.exists()
    assert any(p.name == "a-1.pdf" for p in cfg.trash_dir.rglob("*.pdf"))


def test_rename_suffix(cfg: Config):
    proj = cfg.project_root / "p"
    proj.mkdir(parents=True)
    f = _pdf(proj / "a-2.pdf")
    count = dedupe.rename_files_with_suffix(
        proj, {".pdf"}, cfg, dry_run=False, logger=dedupe.logging.getLogger("t")
    )
    assert count == 1
    assert (proj / "a.pdf").exists()
    assert not f.exists()


def _origin_conn(tmp: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE file_origin (
            file_path TEXT PRIMARY KEY, origin TEXT NOT NULL,
            file_hash TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return conn


def test_copy_md_from_zotero_and_replace_ocr_md(cfg: Config, tmp_path: Path):
    proj = cfg.project_root / "p"
    proj.mkdir(parents=True)
    pdf = _pdf(proj / "doc.pdf")
    zot_md = cfg.library_dir / "doc.md"
    zot_md.write_text("library md content", encoding="utf-8")
    conn = _origin_conn(tmp_path)
    stats = dedupe.copy_md_from_zotero(
        proj, cfg.library_dir, cfg, conn, dry_run=False,
        logger=dedupe.logging.getLogger("t"),
    )
    assert stats["copied"] == 1
    assert (proj / "doc.md").read_text(encoding="utf-8") == "library md content"

    # 模拟旧 OCR 产物：内容不同且来源为 ocr_md -> 应被替换
    (proj / "doc.md").write_text("old ocr output", encoding="utf-8")
    dedupe.set_origin(conn, str(proj / "doc.md"), "ocr_md", "oldhash")
    stats = dedupe.copy_md_from_zotero(
        proj, cfg.library_dir, cfg, conn, dry_run=False,
        logger=dedupe.logging.getLogger("t"),
    )
    assert stats["replaced"] == 1
    assert (proj / "doc.md").read_text(encoding="utf-8") == "library md content"
    assert any(p.name == "doc.md" for p in cfg.trash_dir.rglob("*.md"))
    conn.close()


def test_copy_md_skips_unknown_origin_by_default(cfg: Config, tmp_path: Path):
    proj = cfg.project_root / "p"
    proj.mkdir(parents=True)
    _pdf(proj / "doc.pdf")
    zot_md = cfg.library_dir / "doc.md"
    zot_md.write_text("library md content", encoding="utf-8")
    (proj / "doc.md").write_text("existing md", encoding="utf-8")
    conn = _origin_conn(tmp_path)
    stats = dedupe.copy_md_from_zotero(
        proj, cfg.library_dir, cfg, conn, dry_run=False,
        logger=dedupe.logging.getLogger("t"),
    )
    assert stats["replaced"] == 0
    assert (proj / "doc.md").read_text(encoding="utf-8") == "existing md"
    conn.close()


def test_pdf_md_pair_pdf_moved_to_trash(cfg: Config):
    d = cfg.project_root / "p"
    d.mkdir(parents=True)
    pdf = _pdf(d / "doc.pdf")
    (d / "doc.md").write_text("md", encoding="utf-8")
    count = dedupe.process_pdf_md_pairs(
        d, cfg, dry_run=False, logger=dedupe.logging.getLogger("t")
    )
    assert count == 1
    assert not pdf.exists()
    assert (d / "doc.md").exists()
    assert any(p.name == "doc.pdf" for p in cfg.trash_dir.rglob("*.pdf"))


def test_run_dedupe_dry_run_does_not_move(cfg: Config):
    proj = cfg.project_root / "p"
    proj.mkdir(parents=True)
    pdf = _pdf(proj / "a.pdf")
    (cfg.library_dir / "a.md").write_text("md", encoding="utf-8")
    (proj / "a.md").write_text("md", encoding="utf-8")
    stats = dedupe.run_dedupe(cfg, dry_run=True, scope="all",
                              logger=dedupe.logging.getLogger("t"))
    assert pdf.exists()
    assert not list(cfg.trash_dir.rglob("*")) if cfg.trash_dir.exists() else True
    assert stats["pdf_md_cleanup"] == 1

