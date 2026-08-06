from __future__ import annotations

import sqlite3
from pathlib import Path

from kbimporter import scanner
from kbimporter.config import Config


def _make_academic(cfg: Config, name: str = "贺雪峰 - 2013 - 小农立场.md") -> Path:
    fp = cfg.library_dir / name
    fp.write_text("test", encoding="utf-8")
    return fp


def test_classify_academic(cfg: Config):
    fp = _make_academic(cfg)
    info = scanner.classify_file(fp, cfg)
    assert info["kb_type"] == "academic"
    assert info["collection"] == "academic_library"
    assert info["author"] == "贺雪峰"
    assert info["year"] == 2013
    assert info["title"] == "小农立场"


def test_classify_academic_skips_underscore(cfg: Config):
    fp = _make_academic(cfg, "_项目信息.md")
    assert scanner.classify_file(fp, cfg) is None


def test_classify_project(cfg: Config, monkeypatch):
    monkeypatch.setattr(scanner, "to_pinyin", lambda s: "cunganbuleixing")
    proj = cfg.project_root / "村干部类型"
    proj.mkdir()
    fp = proj / "贺雪峰 - 2024 - 论农村基层治理现代化2.0版.md"
    fp.write_text("x", encoding="utf-8")
    info = scanner.classify_file(fp, cfg)
    assert info["kb_type"] == "project"
    assert info["collection"] == "proj_cunganbuleixing"
    assert info["project_name"] == "村干部类型"
    assert info["author"] == "贺雪峰"


def test_classify_fieldwork_note_and_supplement(cfg: Config):
    proj = cfg.fieldwork_root / "26年枝江仙女镇向巷村"
    note_dir = proj / "笔记"
    sup_dir = proj / "其他材料"
    note_dir.mkdir(parents=True)
    sup_dir.mkdir(parents=True)
    note = note_dir / "26vij1fhtj031001.md"
    sup = sup_dir / "26vij2tlvh033001_资产负债表.md"
    note.write_text("x", encoding="utf-8")
    sup.write_text("y", encoding="utf-8")
    n = scanner.classify_file(note, cfg)
    s = scanner.classify_file(sup, cfg)
    assert n["source_type"] == "note"
    assert s["source_type"] == "supplement"
    assert n["project_name"] == "26年枝江仙女镇向巷村"
    assert n["collection"] == "fieldwork_kb"


def test_parse_academic_filename():
    assert scanner.parse_academic_filename("Aakvaag - 2013 - Social mechanisms.md") == {
        "author": "Aakvaag", "year": 2013, "title": "Social mechanisms",
    }
    fallback = scanner.parse_academic_filename("Bech - Merleau-ponty.md")
    assert fallback["author"] == ""
    assert fallback["year"] == 0
    assert fallback["title"] == "Bech - Merleau-ponty"


def test_detect_language():
    assert scanner.detect_language("贺雪峰 - 2024 - 基层治理.md") == "zh"
    assert scanner.detect_language("Abbott - 1988 - Transcending.md") == "en"


def test_parse_project_info(tmp_path: Path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "_项目信息.md").write_text(
        "# 项目信息\n\n## 基本信息\n- 调研地点：湖北省宜昌市\n- 调研时间：2026年3月\n- 调研人员：邓汉清\n\n## 备注\n这是一个测试。",
        encoding="utf-8",
    )
    info = scanner.parse_project_info(proj)
    assert info["location"] == "湖北省宜昌市"
    assert info["research_date"] == "2026年3月"
    assert info["researchers"] == "邓汉清"
    assert "测试" in info["notes"]


def test_state_helpers(tmp_path: Path):
    conn = scanner.init_db(tmp_path / "state.db")
    scanner.mark_processing(conn, "/a.md", "h1", "academic_library")
    assert scanner.get_file_state(conn, "/a.md")["status"] == "processing"
    scanner.mark_done(conn, "/a.md", "h2", "academic_library", 3)
    st = scanner.get_file_state(conn, "/a.md")
    assert st["hash"] == "h2"
    scanner.set_origin(conn, "/a.md", "ocr_md", "h2")
    assert scanner.get_origin(conn, "/a.md") == "ocr_md"
    scanner.mark_deleted(conn, "/a.md")
    assert scanner.get_deleted_records(conn)
    scanner.purge_deleted_records(conn, ["/a.md"])
    assert not scanner.get_deleted_records(conn)
    conn.close()


def test_scan_all_files(cfg: Config):
    _make_academic(cfg)
    proj = cfg.project_root / "村干部类型"
    proj.mkdir()
    (proj / "a.md").write_text("x", encoding="utf-8")
    notes = cfg.fieldwork_root / "p1" / "笔记"
    notes.mkdir(parents=True)
    (notes / "b.md").write_text("x", encoding="utf-8")
    files = scanner.scan_all_files(cfg)
    assert len(files) == 3


def test_init_db_does_not_alter_existing_legacy_db(tmp_path: Path):
    """复用旧 import_state.db 时，不应向旧库添加新表。"""
    db = tmp_path / "import_state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE file_state (file_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'done', last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "collection_name TEXT NOT NULL, chunk_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE project_meta_state (project_dir TEXT PRIMARY KEY, meta_hash TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    conn2 = scanner.init_db(db)
    tables = {
        r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn2.close()
    assert "file_origin" not in tables
    assert scanner.get_origin(sqlite3.connect(db), "/a.md") is None
