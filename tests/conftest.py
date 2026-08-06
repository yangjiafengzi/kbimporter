from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from kbimporter.config import Config


class _FakeCollection:
    def __init__(self, name: str = "fake"):
        self.name = name
        self.inserted: list[dict] = []
        self.deleted: list[str] = []
        self.upserted: list[dict] = []
        self.flushed = False
        self.loaded = False

    def load(self):
        self.loaded = True

    def insert(self, rows):
        start = len(self.inserted) + 1
        self.inserted.extend(rows)
        pks = list(range(start, start + len(rows)))
        return types.SimpleNamespace(primary_keys=pks)

    def delete(self, expr: str):
        self.deleted.append(expr)

    def upsert(self, rows, partial_update=False):
        self.upserted.extend(rows)

    def flush(self):
        self.flushed = True


class _FakeUtility:
    def has_collection(self, name: str) -> bool:
        return True

    def list_collections(self):
        return []

    def drop_collection(self, name: str):
        pass


class _FakePymilvus(types.ModuleType):
    utility = _FakeUtility()
    Collection = _FakeCollection


if "pymilvus" not in sys.modules:
    sys.modules["pymilvus"] = _FakePymilvus("pymilvus")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    kb_root = tmp_path / "知识库"
    (kb_root / "zotero文献库" / "library").mkdir(parents=True)
    (kb_root / "项目文献").mkdir(parents=True)
    (kb_root / "田野调查笔记").mkdir(parents=True)
    c = Config(
        kb_root=kb_root,
        zotero_storage=tmp_path / "zotero-storage",
        state_dir=kb_root / ".kb",
        trash_dir=kb_root / ".kb" / "trash",
        ocr_work_dir=kb_root / "ocr" / "_convert_work",
    )
    c.derive()
    return c

