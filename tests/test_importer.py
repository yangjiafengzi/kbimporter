from __future__ import annotations

import types
import sys
import sqlite3
from pathlib import Path

import pytest

from kbimporter import importer, models, scanner
from kbimporter.config import Config


class FakeColl:
    def __init__(self, name: str):
        self.name = name
        self.inserted: list[dict] = []
        self.deleted: list[str] = []
        self.upserted: list[dict] = []
        self.flushed = False
        self.released = False

    def load(self):
        pass

    def insert(self, rows):
        start = len(self.inserted) + 1
        self.inserted.extend(rows)
        return list(range(start, start + len(rows)))

    def delete(self, expr: str):
        self.deleted.append(expr)

    def upsert(self, rows, partial_update=False):
        self.upserted.extend(rows)

    def flush(self):
        self.flushed = True

    def release(self):
        self.released = True


@pytest.fixture
def fake_milvus(monkeypatch):
    colls: dict[str, FakeColl] = {}
    monkeypatch.setattr(importer, "_collection_cache", {})

    def get_coll(name, cfg):
        if name not in colls:
            colls[name] = FakeColl(name)
            importer._collection_cache[name] = colls[name]
        return colls[name]

    monkeypatch.setattr(importer, "_get_collection", get_coll)
    monkeypatch.setattr(importer, "_delete_old_vectors", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_cleanup_empty_project_collections", lambda *a, **k: 0)
    for fn in ("ensure_academic_library", "ensure_project_collection", "ensure_fieldwork_kb"):
        monkeypatch.setattr(importer, fn, lambda *a, **k: None)
    return colls


def _seed(cfg: Config):
    academic = cfg.library_dir / "贺雪峰 - 2013 - 小农立场.md"
    academic.write_text("农民问题研究。" * 200, encoding="utf-8")
    proj_dir = cfg.project_root / "村干部类型"
    proj_dir.mkdir(parents=True)
    proj = proj_dir / "贺雪峰 - 2024 - 论农村基层治理现代化.md"
    proj.write_text("基层治理研究。" * 200, encoding="utf-8")
    return academic, proj


def test_import_new_and_incremental_skip(cfg: Config, fake_milvus, monkeypatch):
    monkeypatch.setattr(scanner, "to_pinyin", lambda s: "cunganbuleixing")
    academic, proj = _seed(cfg)

    stats = importer.run_import(cfg, dry_run=False)
    assert stats["new"] == 2
    assert stats["skipped"] == 0
    assert fake_milvus["academic_library"].inserted
    assert fake_milvus["proj_cunganbuleixing"].inserted
    assert fake_milvus["academic_library"].released is True
    assert fake_milvus["proj_cunganbuleixing"].released is True
    assert cfg.state_db.exists()

    stats2 = importer.run_import(cfg, dry_run=False)
    assert stats2["new"] == 0
    assert stats2["skipped"] == 2
    n_before = len(fake_milvus["academic_library"].inserted)
    academic.write_text(academic.read_text(encoding="utf-8") + "新增内容。" * 50, encoding="utf-8")
    stats3 = importer.run_import(cfg, dry_run=False)
    assert stats3["modified"] == 1
    assert len(fake_milvus["academic_library"].inserted) > n_before


def test_import_detects_deleted_files(cfg: Config, fake_milvus, monkeypatch):
    monkeypatch.setattr(scanner, "to_pinyin", lambda s: "cunganbuleixing")
    academic, _ = _seed(cfg)
    importer.run_import(cfg, dry_run=False)
    academic.unlink()
    stats = importer.run_import(cfg, dry_run=False)
    assert stats["deleted"] == 1
    assert any("source_file" in expr for expr in fake_milvus["academic_library"].deleted)


def test_import_dry_run_is_read_only(cfg: Config, fake_milvus, monkeypatch):
    monkeypatch.setattr(scanner, "to_pinyin", lambda s: "cunganbuleixing")
    _seed(cfg)
    stats = importer.run_import(cfg, dry_run=True)
    assert stats["new"] == 2
    assert not cfg.state_db.exists()
    assert fake_milvus == {}


def test_source_rel_path_cross_platform():
    root = Path("/kb")
    assert importer._source_rel_path("/kb/a/b.md", root) == "a/b.md"
    # 不在知识库根下时回退为原样，避免误删其他集合数据
    assert importer._source_rel_path("/other/a.md", root) == "/other/a.md"


def test_milvus_error_hint_covers_function_errors():
    assert "kb doctor --deep" in importer._milvus_error_hint(
        RuntimeError("check function [text_dense_emb:TextEmbedding] failed: 404 Not Found")
    )
    assert importer._milvus_error_hint(RuntimeError("磁盘空间不足")) == ""


def test_import_empty_file_marked(cfg: Config, fake_milvus):
    empty = cfg.library_dir / "Empty - 2020 - Nothing.md"
    empty.write_text("   \n  ", encoding="utf-8")
    stats = importer.run_import(cfg, dry_run=False)
    assert stats["empty"] == 1
    assert not fake_milvus.get("academic_library")


def test_release_loaded_collections_clears_cache(monkeypatch):
    class _Coll:
        def __init__(self, name: str):
            self.name = name
            self.released = False

        def release(self):
            self.released = True

    a, b = _Coll("a"), _Coll("b")
    monkeypatch.setattr(importer, "_collection_cache", {"a": a, "b": b})
    log = importer.logging.getLogger("t")
    assert importer._release_loaded_collections(log) == 2
    assert a.released is True
    assert b.released is True
    assert importer._collection_cache == {}


def test_import_reuses_legacy_state_without_altering_schema(cfg: Config,
                                                            fake_milvus,
                                                            monkeypatch):
    """把 state_db 指向旧 import_state.db 时，可复用且不新增 file_origin 表。"""
    import sqlite3
    from pathlib import Path
    legacy = cfg.kb_root / "0向量化" / "import_state.db"
    legacy.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy)
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
    done = cfg.library_dir / "贺雪峰 - 2013 - 小农立场.md"
    done.write_text("旧内容", encoding="utf-8")
    old_hash = importer.sha256_file(done)
    conn.execute(
        "INSERT INTO file_state VALUES (?, ?, 'done', datetime('now'), 'academic_library', 3)",
        (str(done), old_hash),
    )
    conn.commit()
    conn.close()
    cfg.state_db_path = legacy

    stats = importer.run_import(cfg, dry_run=False)
    assert stats["skipped"] == 1  # 旧状态里的文件被识别为已导入
    check = sqlite3.connect(legacy)
    tables = {
        r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "file_origin" not in tables
    check.close()


def test_cleanup_only_drops_empty_project_collections(cfg, monkeypatch):
    class _FakeClient:
        def __init__(self):
            self.dropped = []

        def list_collections(self, **kwargs):
            return ["proj_empty", "proj_full"]

        def get_collection_stats(self, collection_name, **kwargs):
            return {"row_count": 10 if collection_name == "proj_full" else 0}

        def drop_collection(self, collection_name, **kwargs):
            self.dropped.append(collection_name)

    class _FakePymilvus(types.ModuleType):
        MilvusClient = _FakeClient

    monkeypatch.setitem(sys.modules, "pymilvus", _FakePymilvus("pymilvus"))
    client = _FakeClient()
    monkeypatch.setattr(models, "get_client", lambda cfg: client)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE file_state (
            file_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            collection_name TEXT NOT NULL, chunk_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO file_state VALUES ('/x.md', 'h', 'done', datetime('now'), 'proj_other', 1)"
    )
    conn.commit()
    dropped = importer._cleanup_empty_project_collections(
        conn, cfg, importer.logging.getLogger("t")
    )
    assert dropped == 1
    assert client.dropped == ["proj_empty"]
    conn.close()
