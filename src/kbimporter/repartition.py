from __future__ import annotations

import logging
from collections import defaultdict

from kbimporter.config import Config
from kbimporter.models import ensure_connected, ensure_partitions
from kbimporter.partition import academic_partition_name

_DENSE_FN = "text_dense_emb"

_EXPORT_FIELDS = [
    "id", "text", "source_file", "chunk_index", "granularity",
    "parent_id", "created_at", "language", "author", "year", "title", "vector",
]


def plan_moves(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        # 不 int(year)：year_bucket 内部安全兜底，坏年份 → unknown
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
            yield from batch
    finally:
        try:
            it.close()
        except Exception:
            pass


def _insert_without_id(client, collection_name: str, part: str, rows: list[dict],
                       batch_size: int) -> list[tuple[int, int]]:
    """按批插入（不带 id，auto_id 重新发号）；返回 (old_id, new_id) 对。"""
    pairs: list[tuple[int, int]] = []
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        payload = [{k: r.get(k) for k in _EXPORT_FIELDS if k != "id"} for r in batch]
        res = client.insert(
            collection_name=collection_name, data=payload, partition_name=part,
        )
        new_ids = res.get("ids", []) if isinstance(res, dict) else []
        if len(new_ids) != len(batch):
            raise RuntimeError(f"insert 返回 id 数量不符: {len(new_ids)} != {len(batch)}")
        pairs.extend((old.get("id"), new) for old, new in zip(batch, new_ids))
    return pairs


def _add_dense_function(client, collection_name: str, cfg: Config) -> None:
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
    client.add_collection_function(collection_name=collection_name, function=fn)


def run_repartition(
    cfg: Config,
    dry_run: bool = True,
    collection: str = "academic_library",
    batch_size: int = 200,
    logger: logging.Logger | None = None,
) -> int:
    log = logger or logging.getLogger("kbimporter")
    target = (collection or "academic_library").replace("-", "_")
    if target != "academic_library":
        log.error("当前仅支持 academic_library（收到: %s）", target)
        return 2

    client = ensure_connected(cfg)
    if not client.has_collection(collection_name=target):
        log.error("集合不存在: %s", target)
        return 2
    client.load_collection(collection_name=target)

    rows = list(_iter_rows(client, target, batch_size))
    total = len(rows)
    groups = plan_moves(rows)
    coarse_by_part = {
        p: [r for r in rs if r.get("granularity") == "coarse"] for p, rs in groups.items()
    }
    fine_by_part = {
        p: [r for r in rs if r.get("granularity") != "coarse"] for p, rs in groups.items()
    }

    stats = client.get_collection_stats(collection_name=target) or {}
    log.info("扫描完成: %d 行（stats row_count=%s）", total, stats.get("row_count"))
    parts_seen = sorted(groups)
    log.info("目标分区: %s", ", ".join(parts_seen) or "(none)")
    for part in parts_seen:
        log.info(
            "  %s: coarse=%d fine=%d",
            part, len(coarse_by_part.get(part, ())), len(fine_by_part.get(part, ())),
        )

    if dry_run:
        log.info("dry-run：未创建分区、未写入、未删除。加 --execute 才会真正迁移。")
        return 0

    client.drop_collection_function(collection_name=target, function_name=_DENSE_FN)
    log.info("已临时移除 Function %s", _DENSE_FN)
    try:
        id_map: dict[int, int] = {}
        migrated = 0
        ensure_partitions(client, target, parts_seen)

        for part in parts_seen:
            coarse = coarse_by_part.get(part, [])
            if not coarse:
                continue
            for old_id, new_id in _insert_without_id(
                client, target, part, coarse, batch_size
            ):
                id_map[int(old_id)] = int(new_id)
            migrated += len(coarse)
            log.info("  coarse → %s: %d 行", part, len(coarse))

        for part in parts_seen:
            fine = fine_by_part.get(part, [])
            if not fine:
                continue
            rewritten = rewrite_parent_ids(fine, id_map)
            _insert_without_id(client, target, part, rewritten, batch_size)
            migrated += len(fine)
            log.info("  fine → %s: %d 行", part, len(fine))

        if parts_seen:
            client.load_partitions(collection_name=target, partition_names=parts_seen)

        # 先校验再删 _default：失败时旧数据仍在，可回滚
        if migrated != total:
            raise RuntimeError(f"行数校验失败: 导出 {total}, 写入 {migrated}")
        log.info("校验通过: 导出/写入均为 %d 行", total)

        # 只清 _default：不带 partition_name 的 "id >= 0" 会连新建分区一起删掉
        client.delete(collection_name=target, filter="id >= 0", partition_name="_default")
        log.info("已清空 _default 旧数据")

        _add_dense_function(client, target, cfg)
        log.info("已恢复 Function %s", _DENSE_FN)
        log.info("迁移完成：%d 行，0 次 DashScope 嵌入调用", total)
        return 0
    except Exception as e:
        log.error("迁移失败: %s", e)
        log.error("正在尝试恢复 Function %s，请人工检查新建分区后重试或回滚", _DENSE_FN)
        try:
            _add_dense_function(client, target, cfg)
        except Exception:
            pass
        return 1
