from __future__ import annotations

import re
import sys
import types

import pytest

from kbimporter import models


class _DataType:
    INT64 = "INT64"
    INT32 = "INT32"
    VARCHAR = "VARCHAR"
    FLOAT_VECTOR = "FLOAT_VECTOR"
    SPARSE_FLOAT_VECTOR = "SPARSE_FLOAT_VECTOR"


class _FunctionType:
    TEXTEMBEDDING = "TEXTEMBEDDING"
    BM25 = "BM25"


class _Schema:
    def __init__(self, **kwargs):
        self.description = kwargs.get("description", "")
        self.fields: list[dict] = []
        self.functions: list[object] = []

    def add_field(self, field_name, datatype, **kwargs):
        self.fields.append({"name": field_name, "datatype": datatype, **kwargs})

    def add_function(self, func):
        self.functions.append(func)


class _IndexParams:
    def __init__(self):
        self.indexes: list[dict] = []

    def add_index(self, field_name, index_type="", **kwargs):
        self.indexes.append({"field_name": field_name, "index_type": index_type, **kwargs})


class _FakeClient:
    def __init__(self, uri="", **kwargs):
        self.uri = uri
        self.collections: dict[str, dict] = {}
        self.created: list[str] = []
        self.indexes: list[tuple[str, list[dict]]] = []
        self.released: list[str] = []

    @staticmethod
    def create_schema(**kwargs):
        return _Schema(**kwargs)

    def prepare_index_params(self):
        return _IndexParams()

    def has_collection(self, collection_name, **kwargs):
        return collection_name in self.collections

    def create_collection(self, collection_name, schema=None, **kwargs):
        self.collections[collection_name] = {"rows": [], "schema": schema}
        self.created.append(collection_name)

    def create_index(self, collection_name, index_params=None, **kwargs):
        self.indexes.append((collection_name, index_params.indexes))

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

    def query(self, collection_name, filter="", output_fields=None, limit=None, **kwargs):
        rows = self.collections[collection_name]["rows"]
        match = re.match(r"(\w+)\s*==\s*'([^']*)'", filter or "")
        field, value = (match.group(1), match.group(2)) if match else (None, None)
        out = []
        for r in rows:
            if field is not None and str(r.get(field)) != value:
                continue
            out.append({k: r[k] for k in (output_fields or list(r)) if k in r})
            if limit and len(out) >= limit:
                break
        return out

    def flush(self, collection_name, **kwargs):
        pass

    def load_collection(self, collection_name, **kwargs):
        pass

    def release_collection(self, collection_name, **kwargs):
        self.released.append(collection_name)

    def get_collection_stats(self, collection_name, **kwargs):
        rows = self.collections.get(collection_name, {}).get("rows", [])
        return {"row_count": len(rows)}


def _install_fake_pymilvus(monkeypatch):
    fake = types.ModuleType("pymilvus")
    fake.MilvusClient = _FakeClient
    fake.DataType = _DataType
    fake.FunctionType = _FunctionType
    fake.Function = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "pymilvus", fake)
    monkeypatch.setattr(models, "_client_cache", {})
    return fake


def test_ensure_academic_library_uses_milvus_client(cfg, monkeypatch):
    _install_fake_pymilvus(monkeypatch)
    coll = models.ensure_academic_library(cfg)
    client = models.get_client(cfg)
    assert coll.name == "academic_library"
    assert client.created == ["academic_library"]
    schema = client.collections["academic_library"]["schema"]
    names = {f["name"] for f in schema.fields}
    assert {"id", "text", "vector", "sparse", "language"} <= names
    assert len(schema.functions) == 2
    indexed = {idx["field_name"] for _, idxs in client.indexes for idx in idxs}
    assert {"vector", "sparse", "language"} <= indexed


def test_milvus_collection_crud_wrapper(cfg, monkeypatch):
    _install_fake_pymilvus(monkeypatch)
    coll = models.ensure_academic_library(cfg)
    ids = coll.insert([
        {
            "text": "农民问题研究",
            "source_file": "a.md",
            "chunk_index": 0,
            "granularity": "coarse",
            "parent_id": 0,
            "created_at": 1,
            "language": "zh",
            "author": "贺雪峰",
            "year": 2013,
            "title": "小农立场",
        },
    ])
    assert ids == [1]
    assert coll.num_entities == 1
    rows = coll.query(expr="source_file == 'a.md'", output_fields=["title"])
    assert rows == [{"title": "小农立场"}]
    coll.delete(expr="source_file == 'a.md'")
    assert coll.num_entities == 0
    coll.flush()
    client = models.get_client(cfg)
    coll.release()
    assert client.released == ["academic_library"]


def test_tcp_reachable(monkeypatch):
    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(models.socket, "create_connection", lambda addr, timeout: _Conn())
    assert models.tcp_reachable("localhost", 19530, 2.0) is True

    def _fail(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(models.socket, "create_connection", _fail)
    assert models.tcp_reachable("localhost", 19530, 2.0) is False


def test_get_client_unreachable_raises_friendly(cfg, monkeypatch):
    _install_fake_pymilvus(monkeypatch)
    monkeypatch.setattr(models, "_tcp_reachable", lambda cfg, timeout=2.0: False)
    with pytest.raises(RuntimeError, match="无法连接 Milvus"):
        models.get_client(cfg)


def test_get_client_constructor_error_raises_friendly(cfg, monkeypatch):
    _install_fake_pymilvus(monkeypatch)
    monkeypatch.setattr(models, "_tcp_reachable", lambda cfg, timeout=2.0: True)

    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("grpc unavailable")

    fake = types.ModuleType("pymilvus")
    fake.MilvusClient = _Boom
    monkeypatch.setattr(models, "_pm", lambda: fake)
    with pytest.raises(RuntimeError, match="连接 Milvus 失败"):
        models.get_client(cfg)
