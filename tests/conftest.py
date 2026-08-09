from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

from kbimporter.config import Config


class _FakeSchema:
    def __init__(self, **kwargs):
        self.description = kwargs.get("description", "")
        self.fields: list[dict] = []
        self.functions: list[object] = []

    def add_field(self, field_name, datatype, **kwargs):
        self.fields.append({"name": field_name, "datatype": datatype, **kwargs})

    def add_function(self, func):
        self.functions.append(func)


class _FakeIndexParams:
    def __init__(self):
        self.indexes: list[dict] = []

    def add_index(self, field_name, index_type="", index_name="", **kwargs):
        self.indexes.append({"field_name": field_name, "index_type": index_type, **kwargs})


class _FakeMilvusClient:
    """最小可用的 MilvusClient 替身，覆盖本项目用到的 API。"""

    def __init__(self, uri="", **kwargs):
        self.uri = uri
        self.collections: dict[str, dict] = {}

    @staticmethod
    def create_schema(**kwargs):
        return _FakeSchema(**kwargs)

    def prepare_index_params(self):
        return _FakeIndexParams()

    def has_collection(self, collection_name, **kwargs):
        return collection_name in self.collections

    def list_collections(self, **kwargs):
        return list(self.collections)

    def create_collection(self, collection_name, schema=None, **kwargs):
        self.collections[collection_name] = {"schema": schema, "rows": []}

    def drop_collection(self, collection_name, **kwargs):
        self.collections.pop(collection_name, None)

    def insert(self, collection_name, data, **kwargs):
        rows = self.collections[collection_name]["rows"]
        start = len(rows) + 1
        rows.extend(data)
        return {"insert_count": len(data), "ids": list(range(start, start + len(data)))}

    def delete(self, collection_name, filter=None, **kwargs):
        rows = self.collections[collection_name]["rows"]
        match = re.match(r"(\w+)\s*==\s*'([^']*)'", filter or "")
        field, value = (match.group(1), match.group(2)) if match else (None, None)
        self.collections[collection_name]["rows"] = [
            r for r in rows if field is None or str(r.get(field)) != value
        ]

    def upsert(self, collection_name, data, **kwargs):
        self.collections[collection_name]["rows"].extend(data)

    def query(self, collection_name, filter="", output_fields=None, limit=None, **kwargs):
        rows = self.collections[collection_name]["rows"]
        out = []
        for r in rows:
            if filter and str(filter) not in str(r):
                continue
            out.append({k: r[k] for k in (output_fields or list(r)) if k in r})
            if limit and len(out) >= limit:
                break
        return out

    def search(self, collection_name, data=None, filter="", limit=10,
               output_fields=None, search_params=None, anns_field=None, **kwargs):
        rows = self.collections[collection_name]["rows"]
        hits = [
            {
                "id": i + 1,
                "distance": round(0.9 - i * 0.01, 4),
                "entity": {k: r[k] for k in (output_fields or list(r)) if k in r},
            }
            for i, r in enumerate(rows)
        ]
        return [hits[:limit]]

    def flush(self, collection_name, **kwargs):
        pass

    def load_collection(self, collection_name, **kwargs):
        pass

    def get_collection_stats(self, collection_name, **kwargs):
        rows = self.collections.get(collection_name, {}).get("rows", [])
        return {"row_count": len(rows)}

    def create_index(self, collection_name, index_params=None, **kwargs):
        pass

    def close(self):
        pass


class _FakePymilvus(types.ModuleType):
    MilvusClient = _FakeMilvusClient


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


@pytest.fixture(autouse=True)
def _clear_cloud_quota_flags():
    from kbimporter import cloud_ocr
    cloud_ocr._clear_quota_flags()
    from kbimporter.progress import tracker
    tracker.reset()
    yield
