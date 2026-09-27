from __future__ import annotations

import logging
from collections import defaultdict

from kbimporter.config import Config
from kbimporter.models import ensure_connected
from kbimporter.partition import academic_partition_name

_DENSE_FN = "text_dense_emb"

_EXPORT_FIELDS = [
    "id", "text", "source_file", "chunk_index", "granularity",
    "parent_id", "created_at", "language", "author", "year", "title", "vector",
]


def plan_moves(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name = academic_partition_name(r.get("language"), r.get("year") or 0)
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


def _row_for_insert(row: dict) -> dict:
    return {k: row.get(k) for k in _EXPORT_FIELDS if k != "id"}


def _dense_function(cfg: Config):
    from pymilvus import Function, FunctionType

    return Function(
        name=_DENSE_FN,
        input_field_names=["text"],
        output_field_names=["vector"],
        function_type=FunctionType.TEXTEMBEDDING,
        params={
            "provider": cfg.milvus.embedding_provider,
            "model_name": cfg.milvus.embedding_model,
        },
    )


def run_repartition(
    cfg: Config,
    dry_run: bool = True,
    collection: str = "academic_library",
    batch_size: int = 200,
    logger: logging.Logger | None = None,
) -> int:
    log = logger or logging.getLogger("kbimporter")
    collection = (collection or "academic_library").replace("-", "_")
    if collection != "academic_library":
        log.error("当前仅支持 academic_library")
        return 2

    client = ensure_connected(cfg)
    if not client.has_collection(collection_name=collection):
        log.error("集合不存在: %s", collection)
        return 2

    coarse_by_part: dict[str, list[dict]] = defaultdict(list)
    fine_all: list[dict] = []
    total = 0
    for row in _iter_rows(client, collection, batch_size):
        total += 1
        part = academic_partition_name(row.get("language"), row.get("year") or 0)
        if row.get("granularity") == "coarse":
            coarse_by_part[part].append(row)
        else:
            fine_all.append(row)
        if total % 10000 == 0:
            log.info("  已扫描 %d 行…", total)

    stats = client.get_collection_stats(collection_name=collection) or {}
    log.info("扫描完成: %d 行（stats row_count=%s）", total, stats.get("row_count"))

    fine_by_part: dict[str, list[dict]] = defaultdict(list)
    for r in fine_all:
        fine_by_part[academic_partition_name(r.get("language"), r.get("year") or 0)].append(r)

    parts_seen = sorted(set(coarse_by_part) | set(fine_by_part))
    log.info("目标分区: %s", ", ".join(parts_seen) or "(none)")
    for p in parts_seen:
        log.info("  %s: coarse=%d fine=%d", p, len(coarse_by_part.get(p, [])), len(fine_by_part.get(p, [])))

    if dry_run:
        log.info("dry-run：未创建分区、未写入、未删除。加 --execute 后才会真正迁移。")
        return 0

    client.drop_collection_function(collection_name=collection, function_name=_DENSE_FN)
    log.info("已临时移除 Function %s", _DENSE_FN)

    try:
        id_map: dict[int, int] = {}
        for part, rows in coarse_by_part.items():
            client.create_partition(collection_name=collection, partition_name=part)
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                res = client.insert(
                    collection_name=collection,
                    data=[_row_for_insert(r) for r in batch],
                    partition_name=part,
                )
                new_ids = res.get("ids", []) if isinstance(res, dict) else []
                for old, new in zip(batch, new_ids):
                    id_map[int(old["id"])] = int(new)
            log.info("  coarse → %s: %d 行", part, len(rows))

        for part, rows in fine_by_part.items():
            client.create_partition(collection_name=collection, partition_name=part)
            rewritten = rewrite_parent_ids(rows, id_map)
            for i in range(0, len(rewritten), batch_size):
                batch = rewritten[i:i + batch_size]
                client.insert(
                    collection_name=collection,
                    data=[_row_for_insert(r) for r in batch],
                    partition_name=part,
                )
            log.info("  fine → %s: %d 行", part, len(rows))

        # 只清空 _default，绝不波及刚写入的命名分区
        client.delete(
            collection_name=collection,
            filter="id >= 0",
            partition_name="_default",
        )
        log.info("已清空 _default 旧数据")

        client.add_collection_function(collection_name=collection, function=_dense_function(cfg))
        log.info("已恢复 Function %s", _DENSE_FN)
        log.info("迁移完成：0 次 DashScope 嵌入调用")
        return 0
    except Exception as e:
        log.error("迁移失败: %s", e)
        log.error("正在尝试恢复 Function %s，请人工检查新建分区后重试或回滚", _DENSE_FN)
        try:
            client.add_collection_function(collection_name=collection, function=_dense_function(cfg))
        except Exception:
            pass
        return 1
