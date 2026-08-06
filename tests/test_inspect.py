from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from kbimporter import inspect


def _make_legacy(db: Path):
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE file_state (
            file_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            collection_name TEXT NOT NULL, chunk_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        "CREATE TABLE project_meta_state (project_dir TEXT PRIMARY KEY, meta_hash TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO file_state VALUES (?, ?, 'done', datetime('now'), ?, ?)",
        [("/a.md", "h1", "academic_library", 3), ("/b.md", "h2", "fieldwork_kb", 5)],
    )
    conn.execute("INSERT INTO project_meta_state VALUES ('/p', 'm1')")
    conn.commit()
    conn.close()


def test_scan_state_files_finds_legacy_and_configured(cfg):
    legacy = cfg.kb_root / "0向量化" / "import_state.db"
    _make_legacy(legacy)
    cfg.state_db_path = cfg.kb_root / ".kb" / "state.db"
    result = inspect.scan_state_files(cfg)
    assert result["legacy_import_state_db"]["exists"] is True
    assert result["legacy_import_state_db"]["files"] == 2
    assert result["legacy_import_state_db"]["chunks"] == 8
    assert result["configured"]["exists"] is False
    assert result["configured"]["path"] == str(cfg.kb_root / ".kb" / "state.db")
    assert "default_kb_state_db" not in result  # 与 configured 同路径，已去重


def test_scan_milvus_reports_missing_package(cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymilvus", None)
    result = inspect.scan_milvus(cfg)
    assert result["available"] is False
    assert "pymilvus" in result["reason"]


def test_run_scan_returns_sections(cfg, capsys):
    result = inspect.run_scan(cfg)
    assert "state" in result
    assert "milvus" in result


def test_state_collections_reads_references(cfg):
    legacy = cfg.kb_root / "0向量化" / "import_state.db"
    _make_legacy(legacy)
    cfg.state_db_path = legacy
    refs = inspect.state_collections(cfg)
    assert refs["exists"] is True
    assert refs["collections"]["academic_library"]["files"] == 1
    assert refs["collections"]["academic_library"]["chunks"] == 3
