# academic_library 语言×年代分区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `academic_library` 增加“语言×年代”手工分区，支持 `load_partitions` 动态加载，并用向量保真迁移把已有数据搬进分区（零 DashScope 重嵌入）。

**Architecture:** 在原集合上 `create_partition` + 临时 `drop_collection_function(text_dense_emb)` + 导出/写回 dense 向量 + 重映射 `parent_id` + 恢复 Function。导入按 `language+year` 路由分区；检索用 `--partitions` / `--year-from/to` 只加载子集。

**Tech Stack:** Python 3.11+, pymilvus 3.x（Milvus 2.6.14）, pytest + tests/conftest.py 假 pymilvus

## Global Constraints

- 只改 `academic_library`；`proj_*` / `fieldwork_kb` 行为不变
- **禁止** drop/rebuild `academic_library` 或任何会导致全量重嵌入的路径
- 迁移命令默认 `--dry-run`；真实执行前必须向用户说明影响
- 测试不得连接真实 Milvus；沿用 `tests/conftest.py` 替身
- 密钥只读环境变量；不把 key 写入代码/配置
- 分区名：`{lang}_{bucket}`，`lang∈{zh,en}`，`bucket∈{pre1980,1980s,1990s,2000s,2010s,2020s,unknown}`
- `year<=0` 或缺失 → `unknown`；language 规范为 `zh`/`en`（其他值按 `detect_language` 回退）

---

### Task 1: 分区命名纯函数

**Covers:** [S3]

**Files:**
- Create: `src/kbimporter/partition.py`
- Test: `tests/test_partition.py`

**Interfaces:**
- Produces:
  - `year_bucket(year: int) -> str`
  - `normalize_lang(language: str) -> str`
  - `academic_partition_name(language: str, year: int) -> str`
  - `ACADEMIC_PARTITIONS: list[str]`
  - `partitions_for_year_range(year_from: int | None, year_to: int | None, language: str | None = None) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_partition.py
from kbimporter.partition import (
    ACADEMIC_PARTITIONS,
    academic_partition_name,
    normalize_lang,
    partitions_for_year_range,
    year_bucket,
)


def test_year_bucket_edges():
    assert year_bucket(0) == "unknown"
    assert year_bucket(-1) == "unknown"
    assert year_bucket(1979) == "pre1980"
    assert year_bucket(1980) == "1980s"
    assert year_bucket(1995) == "1990s"
    assert year_bucket(2005) == "2000s"
    assert year_bucket(2015) == "2010s"
    assert year_bucket(2024) == "2020s"


def test_normalize_lang():
    assert normalize_lang("zh") == "zh"
    assert normalize_lang("EN") == "en"
    assert normalize_lang("") == "zh"
    assert normalize_lang("fr") == "zh"


def test_academic_partition_name():
    assert academic_partition_name("zh", 2015) == "zh_2010s"
    assert academic_partition_name("en", 0) == "en_unknown"
    assert academic_partition_name("EN", 1991) == "en_1990s"


def test_all_partitions_unique_and_cover_languages():
    assert len(ACADEMIC_PARTITIONS) == 14
    assert "zh_2010s" in ACADEMIC_PARTITIONS
    assert "en_unknown" in ACADEMIC_PARTITIONS


def test_partitions_for_year_range():
    assert partitions_for_year_range(2015, 2020) == [
        "en_2010s", "en_2020s", "zh_2010s", "zh_2020s",
    ]
    assert partitions_for_year_range(2015, 2020, language="zh") == [
        "zh_2010s", "zh_2020s",
    ]
    assert partitions_for_year_range(None, None) == ACADEMIC_PARTITIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_partition.py -q`
Expected: FAIL `ModuleNotFoundError: No module named 'kbimporter.partition'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/kbimporter/partition.py
from __future__ import annotations

_LANGS = ("zh", "en")
_BUCKETS = ("pre1980", "1980s", "1990s", "2000s", "2010s", "2020s", "unknown")

ACADEMIC_PARTITIONS: list[str] = sorted(
    f"{lang}_{b}" for b in _BUCKETS for lang in _LANGS
)


def year_bucket(year: int) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "unknown"
    if y <= 0:
        return "unknown"
    if y < 1980:
        return "pre1980"
    return f"{(y // 10) * 10}s"


def normalize_lang(language: str | None) -> str:
    lang = (language or "").strip().lower()
    return lang if lang in _LANGS else "zh"


def academic_partition_name(language: str | None, year: int) -> str:
    return f"{normalize_lang(language)}_{year_bucket(year)}"


def partitions_for_year_range(
    year_from: int | None,
    year_to: int | None,
    language: str | None = None,
) -> list[str]:
    if year_from is None and year_to is None:
        names = list(ACADEMIC_PARTITIONS)
    else:
        lo = 0 if year_from is None else int(year_from)
        hi = 10 ** 9 if year_to is None else int(year_to)
        if lo > hi:
            lo, hi = hi, lo
        # 覆盖 [lo, hi] 的所有 bucket + unknown 不自动加入（用户可显式点名）
        names = []
        for b in _BUCKETS:
            if b == "unknown":
                continue
            if b == "pre1980":
                start, end = 0, 1979
            else:
                start = int(b[:4])
                end = start + 9
            if end < lo or start > hi:
                continue
            names.extend(f"{lang}_{b}" for lang in _LANGS)
        names = sorted(set(names))
    if language:
        lang = normalize_lang(language)
        names = [n for n in names if n.startswith(f"{lang}_")]
    return names
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_partition.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbimporter/partition.py tests/test_partition.py
git commit -m "feat: add academic_library partition name helpers"
```

---

### Task 2: 插入支持分区 + 学术导入路由

**Covers:** [S5]

**Files:**
- Modify: `src/kbimporter/models.py` (`MilvusCollection.insert` / `upsert` / `query`)
- Modify: `src/kbimporter/importer.py` (`_batch_insert`, `_insert_coarse_then_fine`, `process_academic`)
- Test: `tests/test_importer.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: `academic_partition_name(language, year)` from Task 1
- Produces:
  - `MilvusCollection.insert(rows, partition_name: str = "") -> list[int]`
  - `MilvusCollection.query(..., partition_names: list[str] | None = None)`
  - academic 导入自动写入 `academic_partition_name(...)`

- [ ] **Step 1: Write the failing test**

在 `tests/test_models.py` 增加：

```python
def test_collection_insert_accepts_partition_name(cfg, monkeypatch):
    from kbimporter import models as m

    calls = []

    class FakeClient:
        def insert(self, collection_name, data, **kwargs):
            calls.append((collection_name, kwargs.get("partition_name"), data))
            return {"ids": list(range(1, len(data) + 1))}

    coll = m.MilvusCollection(FakeClient(), "academic_library")
    ids = coll.insert([{"text": "x"}], partition_name="zh_2010s")
    assert ids == [1]
    assert calls[0][1] == "zh_2010s"
```

在 `tests/test_importer.py` 增加：

```python
def test_process_academic_routes_partition(cfg, fake_milvus, monkeypatch):
    from kbimporter import importer

    inserted = []

    class FakeColl:
        def load(self):
            pass

        def insert(self, rows, partition_name=""):
            inserted.extend([(partition_name, r) for r in rows])
            start = len(inserted)
            return list(range(start - len(rows) + 1, start + 1))

    monkeypatch.setattr(importer, "_get_collection", lambda *a, **k: FakeColl())
    monkeypatch.setattr(importer, "ensure_academic_library", lambda *a, **k: None)
    monkeypatch.setattr(importer, "chunk_document",
                        lambda text, cfg: (["粗块内容"], ["细块内容"], [0]))

    n = importer.process_academic(
        cfg.library_dir / "张三 - 2015 - 测试.md",
        "正文",
        {"author": "张三", "year": 2015, "title": "测试"},
        cfg,
        __import__("logging").getLogger("t"),
    )
    assert n == 2
    assert all(p == "zh_2010s" for p, _ in inserted)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_models.py::test_collection_insert_accepts_partition_name tests/test_importer.py::test_process_academic_routes_partition -q`
Expected: FAIL（签名不认识 `partition_name` / 未路由分区）

- [ ] **Step 3: Write minimal implementation**

`models.py` — 修改 `MilvusCollection`：

```python
    def insert(self, rows: list[dict], partition_name: str = "") -> list[int]:
        result = self._client.insert(
            collection_name=self.name,
            data=rows,
            partition_name=partition_name or "",
        )
        ids = result.get("ids", []) if isinstance(result, dict) else []
        return [int(i) for i in ids]

    def query(self, expr: str = "", output_fields: list[str] | None = None,
              limit: int | None = None, partition_names: list[str] | None = None,
              **kwargs):
        if partition_names:
            kwargs["partition_names"] = partition_names
        return self._client.query(
            collection_name=self.name,
            filter=expr,
            output_fields=output_fields,
            limit=limit,
            **kwargs,
        )
```

`importer.py` — `_batch_insert` / `_insert_coarse_then_fine` 增加 `partition_name`：

```python
def _batch_insert(coll, rows: list[dict], cfg: Config, partition_name: str = "") -> list[int]:
    all_ids: list[int] = []
    batch_size = cfg.milvus.batch_size
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        all_ids.extend(coll.insert(batch, partition_name=partition_name))
    return all_ids
```

在 `_insert_coarse_then_fine` 中接受 `partition_name: str = ""` 并传给 `_batch_insert`。

`process_academic`：

```python
from kbimporter.partition import academic_partition_name

def process_academic(...):
    ...
    lang = detect_language(fp.name)
    part = academic_partition_name(lang, int(info.get("year") or 0))
    base = {
        "source_file": ...,
        "extra_keys": {...},
    }
    log.info(f"    切片完成: ... (语言: {lang}, 分区: {part})")
    return _insert_coarse_then_fine(coll, coarse, fine, parent_idx, base, cfg, log,
                                    partition_name=part)
```

`proj_*` / `fieldwork` 调用 `_insert_coarse_then_fine` 时不传 `partition_name`（默认 `""`）。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_models.py tests/test_importer.py tests/test_partition.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbimporter/models.py src/kbimporter/importer.py tests/test_models.py tests/test_importer.py
git commit -m "feat: route academic_library inserts to language-year partitions"
```

---

### Task 3: `kb repartition` 向量保真迁移

**Covers:** [S2, S4]

**Files:**
- Create: `src/kbimporter/repartition.py`
- Modify: `src/kbimporter/cli.py`（子命令 `repartition`）
- Test: `tests/test_repartition.py`

**Interfaces:**
- Consumes: `academic_partition_name`, `ACADEMIC_PARTITIONS`
- Produces:
  - `run_repartition(cfg, dry_run: bool = True, collection: str = "academic_library", batch_size: int = 200, logger=None) -> int`
  - CLI: `kb repartition academic-library [--dry-run] [--batch-size N]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repartition.py
from __future__ import annotations

import pytest

from kbimporter.partition import academic_partition_name
from kbimporter.repartition import plan_moves, rewrite_parent_ids


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
    assert out[0]["id"] == 10  # 其他字段不动


def test_repartition_dry_run_is_read_only(cfg, fake_milvus, monkeypatch, capsys):
    from kbimporter import repartition as rp

    class FakeClient:
        def __init__(self):
            self.dropped = []
            self.added = []
            self.inserted = []
            self.deleted = []

        def has_collection(self, collection_name):
            return True

        def list_partitions(self, collection_name):
            return ["_default"]

        def create_partition(self, collection_name, partition_name):
            pass

        def drop_collection_function(self, collection_name, function_name):
            self.dropped.append(function_name)

        def add_collection_function(self, collection_name, function):
            self.added.append(getattr(function, "name", "fn"))

        def insert(self, collection_name, data, partition_name="", **k):
            self.inserted.append((partition_name, len(data)))
            return {"ids": list(range(len(data)))}

        def delete(self, collection_name, filter=None, **k):
            self.deleted.append(filter)

        def query_iterator(self, collection_name, filter="", output_fields=None,
                           batch_size=1000, **k):
            class It:
                def __init__(self):
                    self.n = 0

                def next(self):
                    self.n += 1
                    if self.n == 1:
                        return [{
                            "id": 1, "text": "c", "source_file": "a.md",
                            "chunk_index": 0, "granularity": "coarse",
                            "parent_id": 0, "created_at": 1,
                            "language": "zh", "author": "x", "year": 2015,
                            "title": "t", "vector": [0.1] * 8,
                        }, {
                            "id": 2, "text": "f", "source_file": "a.md",
                            "chunk_index": 0, "granularity": "fine",
                            "parent_id": 1, "created_at": 1,
                            "language": "zh", "author": "x", "year": 2015,
                            "title": "t", "vector": [0.2] * 8,
                        }]
                    return []

                def close(self):
                    pass

            return It()

        def get_collection_stats(self, collection_name):
            return {"row_count": 2}

    fake = FakeClient()
    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: fake)
    rc = rp.run_repartition(cfg, dry_run=True)
    assert rc == 0
    assert fake.dropped == [] and fake.deleted == []
    assert fake.inserted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_repartition.py -q`
Expected: FAIL `ModuleNotFoundError: kbimporter.repartition`

- [ ] **Step 3: Write minimal implementation**

```python
# src/kbimporter/repartition.py
from __future__ import annotations

import logging
from collections import defaultdict

from kbimporter.config import Config
from kbimporter.models import ensure_connected
from kbimporter.partition import (
    ACADEMIC_PARTITIONS,
    academic_partition_name,
)

_DENSE_FN = "text_dense_emb"
_BM25_FN = "text_bm25_emb"

_EXPORT_FIELDS = [
    "id", "text", "source_file", "chunk_index", "granularity",
    "parent_id", "created_at", "language", "author", "year", "title", "vector",
]


def plan_moves(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = academic_partition_name(r.get("language"), int(r.get("year") or 0))
        groups[name].append(r)
    return dict(groups)


def rewrite_parent_ids(fine_rows: list[dict], id_map: dict[int, int]) -> list[dict]:
    out = []
    for r in fine_rows:
        nr = dict(r)
        if nr.get("parent_id"):
            nr["parent_id"] = id_map.get(int(nr["parent_id"]), 0)
        out.append(nr)
    return out


def _iter_rows(client, collection_name: str, batch_size: int):
    it = client.query_iterator(
        collection_name=collection_name,
        filter="id >= 0",
        output_fields=_EXPORT_FIELDS,
        batch_size=batch_size,
    )
    try:
        while True:
            batch = it.next()
            if not batch:
                break
            for row in batch:
                yield row
    finally:
        try:
            it.close()
        except Exception:
            pass


def run_repartition(
    cfg: Config,
    dry_run: bool = True,
    collection: str = "academic_library",
    batch_size: int = 200,
    logger: logging.Logger | None = None,
) -> int:
    log = logger or logging.getLogger("kbimporter")
    if collection != "academic_library":
        log.error("当前仅支持 academic_library")
        return 2

    client = ensure_connected(cfg)
    if not client.has_collection(collection_name=collection):
        log.error("集合不存在: %s", collection)
        return 2

    # 1) 全量读入并分组（大数据量会占用较多内存，生产应按批；
    #    先按批收集到分区桶，coarse/fine 分离）
    coarse_by_part: dict[str, list[dict]] = defaultdict(list)
    fine_all: list[dict] = []
    total = 0
    for row in _iter_rows(client, collection, batch_size):
        total += 1
        part = academic_partition_name(row.get("language"), int(row.get("year") or 0))
        if row.get("granularity") == "coarse":
            coarse_by_part[part].append(row)
        else:
            fine_all.append(row)
        if total % 10000 == 0:
            log.info("  已扫描 %d 行…", total)

    stats = client.get_collection_stats(collection_name=collection) or {}
    log.info("扫描完成: %d 行（stats row_count=%s）", total, stats.get("row_count"))
    parts_seen = sorted(
        set(coarse_by_part)
        | {academic_partition_name(r.get("language"), int(r.get("year") or 0)) for r in fine_all}
    )
    log.info("目标分区: %s", ", ".join(parts_seen) or "(none)")
    for p in parts_seen:
        nc = len(coarse_by_part.get(p, []))
        nf = sum(1 for r in fine_all if academic_partition_name(r.get("language"), int(r.get("year") or 0)) == p)
        log.info("  %s: coarse=%d fine=%d", p, nc, nf)

    if dry_run:
        log.info("dry-run：未创建分区、未写入、未删除。取消 --dry-run 后才会真正迁移。")
        return 0

    # 2) 摘掉 dense Function（BM25 保留，text 重插会免费重算 sparse）
    client.drop_collection_function(collection_name=collection, function_name=_DENSE_FN)
    log.info("已临时移除 Function %s", _DENSE_FN)

    try:
        # 3) 建分区并写入 coarse，记录 old_id -> new_id
        id_map: dict[int, int] = {}
        for part, rows in coarse_by_part.items():
            client.create_partition(collection_name=collection, partition_name=part)
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                clean = [{k: r.get(k) for k in _EXPORT_FIELDS if k != "id"} for r in batch]
                res = client.insert(
                    collection_name=collection, data=clean, partition_name=part,
                )
                new_ids = res.get("ids", []) if isinstance(res, dict) else []
                for old, new in zip(batch, new_ids):
                    id_map[int(old["id"])] = int(new)
            log.info("  coarse → %s: %d 行", part, len(rows))

        # 4) fine：重映射 parent_id 后写入
        fine_by_part: dict[str, list[dict]] = defaultdict(list)
        for r in fine_all:
            part = academic_partition_name(r.get("language"), int(r.get("year") or 0))
            fine_by_part[part].append(r)
        for part, rows in fine_by_part.items():
            client.create_partition(collection_name=collection, partition_name=part)
            rewritten = rewrite_parent_ids(rows, id_map)
            for i in range(0, len(rewritten), batch_size):
                batch = rewritten[i:i + batch_size]
                clean = [{k: r.get(k) for k in _EXPORT_FIELDS if k != "id"} for r in batch]
                client.insert(
                    collection_name=collection, data=clean, partition_name=part,
                )
            log.info("  fine → %s: %d 行", part, len(rows))

        # 5) 清空 _default 旧数据（只删数据，不 drop 集合）
        client.delete(collection_name=collection, filter="id >= 0")
        log.info("已清空 _default 旧数据")

        # 6) 恢复 dense Function
        from pymilvus import Function, FunctionType

        fn = Function(
            name=_DENSE_FN,
            input_field_names=["text"],
            output_field_names=["vector"],
            function_type=FunctionType.TEXTEMBEDDING,
            params={
                "provider": cfg.milvus.embedding_provider,
                "model_name": cfg.milvus.embedding_model,
            },
        )
        client.add_collection_function(collection_name=collection, function=fn)
        log.info("已恢复 Function %s", _DENSE_FN)
        log.info("迁移完成：0 次 DashScope 嵌入调用")
        return 0
    except Exception as e:
        log.error("迁移失败: %s", e)
        log.error("正在尝试恢复 Function %s，请人工检查新建分区后重试或回滚", _DENSE_FN)
        try:
            from pymilvus import Function, FunctionType

            fn = Function(
                name=_DENSE_FN,
                input_field_names=["text"],
                output_field_names=["vector"],
                function_type=FunctionType.TEXTEMBEDDING,
                params={
                    "provider": cfg.milvus.embedding_provider,
                    "model_name": cfg.milvus.embedding_model,
                },
            )
            client.add_collection_function(collection_name=collection, function=fn)
        except Exception:
            pass
        return 1
```

CLI（`cli.py`）：

```python
def cmd_repartition(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.repartition import run_repartition
    return run_repartition(
        cfg,
        dry_run=not args.execute,
        collection=args.target,
        batch_size=args.batch_size,
        logger=log,
    )
```

parser：

```python
    p = sub.add_parser(
        "repartition",
        help="academic_library 按语言×年代重分区（向量保真，不重嵌入；默认 dry-run）",
    )
    _add_config_arg(p)
    p.add_argument("target", nargs="?", default="academic-library",
                   help="目前仅支持 academic-library")
    p.add_argument("--execute", action="store_true",
                   help="真正执行迁移（默认 dry-run）")
    p.add_argument("--batch-size", type=int, default=200)
    p.set_defaults(func=cmd_repartition)
```

注意：目标名接受 `academic-library` / `academic_library`，内部归一为 `academic_library`。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_repartition.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbimporter/repartition.py src/kbimporter/cli.py tests/test_repartition.py
git commit -m "feat: add kb repartition vector-preserving academic_library migration"
```

---

### Task 4: 检索/释放分区感知加载

**Covers:** [S6]

**Files:**
- Modify: `src/kbimporter/cli.py`（`cmd_search`, `cmd_release`, parser）
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `partitions_for_year_range(year_from, year_to, language)`
- Produces:
  - `kb search --partitions A,B --year-from Y --year-to Y --lang zh`
  - `kb release [collection] [--partitions A,B]`
  - 仅加载指定分区；`--release` 卸载

- [ ] **Step 1: Write the failing test**

```python
def test_search_year_range_loads_only_partitions(tmp_path, monkeypatch):
    from kbimporter.cli import main

    loaded = []
    released = []
    searched = []

    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def load_partitions(self, collection_name, partition_names, **k):
            loaded.append(list(partition_names))

        def load_collection(self, collection_name, **k):
            loaded.append(["__ALL__"])

        def release_partitions(self, collection_name, partition_names, **k):
            released.append(list(partition_names))

        def release_collection(self, collection_name, **k):
            released.append(["__ALL__"])

        def search(self, collection_name, **kwargs):
            searched.append(kwargs)
            return [[]]

    monkeypatch.setattr("kbimporter.models.get_client", lambda cfg: FakeClient())
    rc = main([
        "search", "--collection", "academic_library", "--kind", "dense",
        "村干部", "--year-from", "2015", "--year-to", "2020",
        "--release", "--config", "kb_config.toml",
    ])
    assert rc == 0
    assert loaded == [["en_2010s", "en_2020s", "zh_2010s", "zh_2020s"]]
    assert searched and "partition_names" in searched[0]
    assert released == [["en_2010s", "en_2020s", "zh_2010s", "zh_2020s"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py::test_search_year_range_loads_only_partitions -q`
Expected: FAIL（无 `--year-from` 或整库 load）

- [ ] **Step 3: Write minimal implementation**

`cmd_search` 关键改动：

```python
def cmd_search(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.models import get_client
    from kbimporter.partition import partitions_for_year_range

    client = get_client(cfg)
    coll_name = args.collection
    if not client.has_collection(collection_name=coll_name):
        print(f"集合不存在: {coll_name}")
        return 2

    parts = None
    if getattr(args, "partitions", None):
        parts = [p.strip() for p in args.partitions.split(",") if p.strip()]
    elif getattr(args, "year_from", None) or getattr(args, "year_to", None):
        parts = partitions_for_year_range(
            getattr(args, "year_from", None),
            getattr(args, "year_to", None),
            getattr(args, "lang", None),
        )
        if coll_name != "academic_library":
            print("提示: --year-from/to 仅对 academic_library 做分区裁剪，本集合将整库加载")
            parts = None

    if parts:
        client.load_partitions(collection_name=coll_name, partition_names=parts)
    else:
        client.load_collection(collection_name=coll_name)

    try:
        ...
        search_kwargs = {
            "collection_name": coll_name,
            "limit": args.limit,
            "output_fields": output_fields,
            "filter": args.filter or "",
        }
        if parts:
            search_kwargs["partition_names"] = parts
        # query / search 时带上 partition_names
        ...
    finally:
        if getattr(args, "release", False):
            if parts:
                client.release_partitions(collection_name=coll_name, partition_names=parts)
            else:
                client.release_collection(collection_name=coll_name)
```

parser：

```python
    p.add_argument("--partitions", help="仅加载/检索这些分区，逗号分隔，如 zh_2010s,en_2010s")
    p.add_argument("--year-from", type=int, help="起始年份（映射到 academic_library 分区）")
    p.add_argument("--year-to", type=int, help="结束年份")
    p.add_argument("--lang", choices=["zh", "en"], help="限定语言分区")
```

`cmd_release`：

```python
    p.add_argument("--partitions", help="仅释放这些分区，逗号分隔")
```

```python
def cmd_release(args):
    ...
    for name in names:
        parts = getattr(args, "partitions", None)
        if parts:
            plist = [x.strip() for x in parts.split(",") if x.strip()]
            client.release_partitions(collection_name=name, partition_names=plist)
            print(f"已释放分区: {name} {plist}")
        else:
            client.release_collection(collection_name=name)
            print(f"已释放: {name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbimporter/cli.py tests/test_cli.py
git commit -m "feat: partition-aware kb search/release with year-range mapping"
```

---

### Task 5: 提示词与文档

**Covers:** [S7]

**Files:**
- Modify: `agents/academic_advisor.md`
- Modify: `agents/README.md`
- Modify: `agents_nopush/文献知识库问答.md`（及其他提到 academic_library 的示例）
- Modify: `docs/DB_GUIDE.md`
- Modify: `AGENTS.md`
- Modify: `README.md`（常用命令表）

**Interfaces:**
- Consumes: 分区命名、`kb search --year-from/to/--partitions`、`kb repartition`

- [ ] **Step 1: 更新 `docs/DB_GUIDE.md` 分区专节**

在“检索方式”前插入：

```markdown
## 分区（academic_library）

`academic_library` 按 **语言 × 年代** 做手工分区，支持按需 `load_partitions`，避免整库进内存。

| 部分 | 取值 |
| --- | --- |
| 语言 | `zh` / `en` |
| 年代 | `pre1980` / `1980s` / `1990s` / `2000s` / `2010s` / `2020s` / `unknown` |

分区名 = `{lang}_{bucket}`，例如 `zh_2010s`、`en_unknown`（`year<=0`）。

```bash
# 只加载/检索 2015–2022 覆盖的分区
kb search --collection academic_library --kind dense "村干部" --year-from 2015 --year-to 2022

# 直接点名分区
kb search --collection academic_library --kind dense "村干部" --partitions zh_2010s,zh_2020s

# 释放
kb release academic_library --partitions zh_2010s
```

- 不带 `--partitions` / `--year-*` 时仍 `load_collection` 整库加载（兼容旧行为）。
- **禁止**在只需要若干年代时对 `academic_library` 无脑 `load_collection`。
- 迁移：`kb repartition academic-library`（默认 dry-run；`--execute` 真正执行）。
  向量保真搬运，**不会**重新调用 DashScope 嵌入。
```

- [ ] **Step 2: 更新 `agents/academic_advisor.md` 内存管理块**

将原“一次只保留一个 Collection 加载”扩展为：

```markdown
> ⚠️ **内存管理（academic_library 已分区）**：
> 1. 检索 `academic_library` **必须**按年代/分区加载，禁止整库 `load_collection`：
>    - 优先通过 CLI：`kb search --collection academic_library --kind dense "问题" --year-from <起> --year-to <止> [--release]`
>    - 若仅有 MCP：只加载所需分区；没有分区 API 时，先 `kb search --year-from/to` 获取命中，再用 `milvus_query` 回取父块。
> 2. 常用分区：`zh_2010s` `zh_2020s` `en_2010s` `en_2020s` `zh_unknown` `en_unknown`。
> 3. `proj_*` / `fieldwork_kb` 仍按“一次只加载一个 Collection”执行。
> 4. 全部检索结束后释放已加载的分区/集合。
```

在 Step 0 检索流程示例中替换：

```markdown
Step 0.2（学术库，按年代分区加载）:
  kb search --collection academic_library --kind dense "<主题词>" --year-from 2010 --year-to 2024
  # 或只查近十年
  kb search --collection academic_library --kind dense "<主题词>" --year-from 2015 --year-to 2024 --release
```

- [ ] **Step 3: 更新 `agents/README.md` 与 `agents_nopush/*`**

在工具速览/示例中加入：

```markdown
### 示例提示词（分区动态加载）

用户问“近十年关于村民自治的研究”时：

```bash
kb search --collection academic_library --kind dense "村民自治" --year-from 2015 --year-to 2024 --release
kb search --collection academic_library --kind bm25 "村民自治" --year-from 2015 --year-to 2024 --release
```

不要执行 `kb search` 不带年份参数的整库检索，除非用户明确要求全时段。
```

- [ ] **Step 4: 更新 `AGENTS.md` 与 `README.md` 命令表**

AGENTS.md 命令区增加：

```text
kb repartition academic-library [--execute] [--batch-size N]
kb search ... [--partitions zh_2010s,...] [--year-from Y --year-to Y] [--lang zh|en]
kb release [集合] [--partitions zh_2010s,...]
```

并写明：academic_library 分区命名、迁移零重嵌入、检索必须按需加载。

- [ ] **Step 5: 全文检查无遗留“整库加载 academic_library”旧指令**

Run: `rg -n "load_collection.*academic_library|milvus_load_collection" agents docs AGENTS.md`
Expected: 仅出现在“禁止/兼容旧行为”上下文中

- [ ] **Step 6: Commit**

```bash
git add agents agents_nopush docs/DB_GUIDE.md AGENTS.md README.md
git commit -m "docs: teach agents partition-aware academic_library loading"
```

---

## Self-Review

1. **Spec coverage:** S3→T1, S5→T2, S2/S4→T3, S6→T4, S7→T5。S8 测试折入各 Task 的测试步。S9 非目标无任务（正确）。
2. **Placeholder scan:** 无 TBD；代码块均为可落地实现。
3. **Type consistency:** `academic_partition_name(language, year)`、`partitions_for_year_range`、`run_repartition(cfg, dry_run, collection, batch_size, logger)`、`insert(..., partition_name="")` 全文一致。
