from __future__ import annotations

import types

import pymilvus

from kbimporter.repartition import plan_moves, rewrite_parent_ids, run_repartition

# conftest 的假 pymilvus 只有 MilvusClient；补 Function/FunctionType 供恢复 dense Function
if not hasattr(pymilvus, "Function"):
    pymilvus.Function = lambda **kw: types.SimpleNamespace(**kw)
    pymilvus.FunctionType = types.SimpleNamespace(
        TEXTEMBEDDING="TEXTEMBEDDING", BM25="BM25"
    )


def test_plan_moves_groups_by_partition():
    rows = [
        {"id": 1, "parent_id": 0, "language": "zh", "year": 2015, "granularity": "coarse"},
        {"id": 2, "parent_id": 1, "language": "zh", "year": 2015, "granularity": "fine"},
        {"id": 3, "parent_id": 0, "language": "en", "year": 0, "granularity": "coarse"},
    ]
    groups = plan_moves(rows)
    assert set(groups) == {"zh_2010s", "en_unknown"}
    assert [r["id"] for r in groups["zh_2010s"]] == [1, 2]
    assert [r["id"] for r in groups["en_unknown"]] == [3]


def test_rewrite_parent_ids_uses_map():
    fine = [{"id": 10, "parent_id": 1}, {"id": 11, "parent_id": 1}]
    out = rewrite_parent_ids(fine, {1: 100})
    assert [r["parent_id"] for r in out] == [100, 100]
    assert out[0]["id"] == 10


def test_plan_moves_handles_bad_year_string():
    rows = [{"id": 1, "parent_id": 0, "language": "zh", "year": "c.2015", "granularity": "coarse"}]
    groups = plan_moves(rows)
    assert set(groups) == {"zh_unknown"}


def _sample_rows():
    return [
        {
            "id": 1, "text": "c", "source_file": "a.md", "chunk_index": 0,
            "granularity": "coarse", "parent_id": 0, "created_at": 1,
            "language": "zh", "author": "x", "year": 2015, "title": "t",
            "vector": [0.1] * 8,
        },
        {
            "id": 2, "text": "f", "source_file": "a.md", "chunk_index": 0,
            "granularity": "fine", "parent_id": 1, "created_at": 1,
            "language": "zh", "author": "x", "year": 2015, "title": "t",
            "vector": [0.2] * 8,
        },
    ]


class _FakeClient:
    def __init__(self, rows=None):
        self.rows = list(rows if rows is not None else _sample_rows())
        self.dropped = []
        self.added = []
        self.inserted = []
        self.deleted = []
        self.created_parts = []
        self.loaded_parts = []
        self._next_id = 100

    def has_collection(self, collection_name):
        return True

    def load_collection(self, collection_name, **k):
        pass

    def list_partitions(self, collection_name):
        return ["_default"] + self.created_parts

    def create_partition(self, collection_name, partition_name):
        if partition_name not in self.created_parts:
            self.created_parts.append(partition_name)

    def load_partitions(self, collection_name, partition_names, **k):
        self.loaded_parts.append(list(partition_names))

    def drop_collection_function(self, collection_name, function_name):
        self.dropped.append(function_name)

    def add_collection_function(self, collection_name, function):
        self.added.append(getattr(function, "name", "fn"))

    def insert(self, collection_name, data, partition_name="", **k):
        ids = []
        for row in data:
            self._next_id += 1
            ids.append(self._next_id)
            self.inserted.append((partition_name, row))
        return {"ids": ids}

    def delete(self, collection_name, filter=None, partition_name=None, **k):
        self.deleted.append((filter, partition_name))

    def query_iterator(self, collection_name, filter="", output_fields=None,
                       batch_size=1000, **k):
        rows = self.rows

        class It:
            def __init__(self):
                self.n = 0

            def next(self):
                self.n += 1
                if self.n == 1:
                    return list(rows)
                return []

            def close(self):
                pass

        return It()

    def get_collection_stats(self, collection_name):
        return {"row_count": len(self.rows)}


def test_repartition_dry_run_is_read_only(cfg, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda c: fake)
    monkeypatch.setattr("kbimporter.repartition.ensure_connected", lambda c: fake)
    rc = run_repartition(cfg, dry_run=True)
    assert rc == 0
    assert fake.dropped == []
    assert fake.deleted == []
    assert fake.inserted == []
    assert fake.added == []
    assert fake.created_parts == []


def test_repartition_execute_copies_and_only_clears_default(cfg, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda c: fake)
    monkeypatch.setattr("kbimporter.repartition.ensure_connected", lambda c: fake)
    rc = run_repartition(cfg, dry_run=False, batch_size=10)
    assert rc == 0
    assert fake.dropped == ["text_dense_emb"]
    assert fake.added == ["text_dense_emb"]
    # coarse 先插（新 id 101），fine 后插且 parent_id 已重映射
    parts = [p for p, _ in fake.inserted]
    assert parts == ["zh_2010s", "zh_2010s"]
    coarse_row = fake.inserted[0][1]
    fine_row = fake.inserted[1][1]
    assert "id" not in coarse_row and "id" not in fine_row
    assert coarse_row["granularity"] == "coarse"
    assert fine_row["granularity"] == "fine"
    assert fine_row["parent_id"] == 101
    assert coarse_row["vector"] == [0.1] * 8
    assert fine_row["vector"] == [0.2] * 8
    # 只清 _default，不碰新建分区
    assert fake.deleted == [("id >= 0", "_default")]
    assert "zh_2010s" in fake.created_parts


def test_repartition_failure_readds_dense_function(cfg, monkeypatch):
    class _BoomClient(_FakeClient):
        def insert(self, collection_name, data, partition_name="", **k):
            raise RuntimeError("boom")

    fake = _BoomClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda c: fake)
    monkeypatch.setattr("kbimporter.repartition.ensure_connected", lambda c: fake)
    rc = run_repartition(cfg, dry_run=False)
    assert rc == 1
    assert fake.dropped == ["text_dense_emb"]
    assert fake.added == ["text_dense_emb"]


def test_repartition_rejects_other_collections(cfg, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr("kbimporter.repartition.ensure_connected", lambda c: fake)
    assert run_repartition(cfg, dry_run=True, collection="fieldwork_kb") == 2
    assert run_repartition(cfg, dry_run=True, collection="academic-library") == 0


def test_cli_repartition_normalizes_target_and_defaults_dry_run(monkeypatch):
    from kbimporter import repartition as rp
    from kbimporter.cli import main

    seen: dict = {}

    def fake_run(cfg, dry_run=True, collection="academic_library",
                 batch_size=200, logger=None):
        seen.update(dry_run=dry_run, collection=collection, batch_size=batch_size)
        return 0

    monkeypatch.setattr(rp, "run_repartition", fake_run)
    rc = main(["repartition", "academic-library"])
    assert rc == 0
    assert seen == {"dry_run": True, "collection": "academic_library", "batch_size": 200}

    seen.clear()
    rc = main(["repartition", "--execute", "--batch-size", "50"])
    assert rc == 0
    assert seen == {"dry_run": False, "collection": "academic_library", "batch_size": 50}
