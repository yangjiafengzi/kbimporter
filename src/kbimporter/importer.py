from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from kbimporter.chunker import chunk_document
from kbimporter.config import Config
from kbimporter.models import (
    ensure_academic_library,
    ensure_connected,
    ensure_fieldwork_kb,
    ensure_project_collection,
)
from kbimporter.scanner import (
    classify_file,
    detect_language,
    get_all_done_file_states,
    get_deleted_records,
    get_field_project_dir,
    get_file_state,
    get_project_meta_hash,
    init_db,
    mark_deleted,
    mark_done,
    mark_processing,
    parse_project_info,
    purge_deleted_records,
    save_project_meta_hash,
    scan_all_files,
)
from kbimporter.util import read_text_safe, sha256_file


_collection_cache: dict[str, object] = {}


def _source_rel_path(state_path: str, root: Path) -> str:
    """把状态库中的文件路径转换为 Milvus source_file（相对知识库根、正斜杠）。"""
    try:
        return Path(state_path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # 兼容旧状态库中非 root 前缀的历史路径
        return state_path.replace(str(root) + "\\", "").replace("\\", "/")


def _get_collection(coll_name: str, cfg: Config):
    from pymilvus import Collection
    ensure_connected(cfg)
    if coll_name not in _collection_cache:
        coll = Collection(name=coll_name)
        coll.load()
        _collection_cache[coll_name] = coll
    return _collection_cache[coll_name]


def _delete_old_vectors(coll_name: str, source_file: str, cfg: Config):
    from pymilvus import utility
    ensure_connected(cfg)
    if not utility.has_collection(coll_name):
        return
    coll = _get_collection(coll_name, cfg)
    escaped = source_file.replace("'", "\\'")
    coll.delete(f"source_file == '{escaped}'")


def _batch_insert(coll, rows: list[dict], cfg: Config) -> list[int]:
    all_ids: list[int] = []
    batch_size = cfg.milvus.batch_size
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        result = coll.insert(batch)
        all_ids.extend(result.primary_keys)
    return all_ids


def _insert_coarse_then_fine(coll, coarse_chunks: list[str],
                             fine_chunks: list[str], parent_indices: list[int],
                             base_fields: dict, cfg: Config,
                             log: logging.Logger) -> int:
    now = int(time.time())
    source_file = base_fields["source_file"]
    extra_keys = base_fields.get("extra_keys", {})

    coarse_rows = []
    for i, chunk in enumerate(coarse_chunks):
        row = {
            "text": chunk,
            "source_file": source_file,
            "chunk_index": i,
            "granularity": "coarse",
            "parent_id": 0,
            "created_at": now,
        }
        row.update(extra_keys)
        coarse_rows.append(row)
    coarse_ids = _batch_insert(coll, coarse_rows, cfg)
    log.info(f"    粗块已插入: {len(coarse_ids)} 条")

    if not fine_chunks:
        return len(coarse_ids)
    fine_parent_ids = [int(coarse_ids[pi]) for pi in parent_indices]
    fine_rows = []
    for i, chunk in enumerate(fine_chunks):
        row = {
            "text": chunk,
            "source_file": source_file,
            "chunk_index": i,
            "granularity": "fine",
            "parent_id": fine_parent_ids[i],
            "created_at": now,
        }
        row.update(extra_keys)
        fine_rows.append(row)
    fine_ids = _batch_insert(coll, fine_rows, cfg)
    log.info(f"    细块已插入: {len(fine_ids)} 条")
    return len(coarse_ids) + len(fine_ids)


def process_academic(fp: Path, text: str, info: dict, cfg: Config,
                     log: logging.Logger) -> int:
    ensure_academic_library(cfg)
    coll = _get_collection("academic_library", cfg)
    coarse, fine, parent_idx = chunk_document(text, cfg.chunk)
    if not coarse:
        return 0
    lang = detect_language(fp.name)
    base = {
        "source_file": fp.relative_to(cfg.require_kb_root()).as_posix(),
        "extra_keys": {
            "language": lang,
            "author": info.get("author", ""),
            "year": info.get("year", 0),
            "title": info.get("title", ""),
        },
    }
    log.info(f"    切片完成: {len(coarse)} 粗块 + {len(fine)} 细块 (语言: {lang})")
    return _insert_coarse_then_fine(coll, coarse, fine, parent_idx, base, cfg, log)


def process_project(fp: Path, text: str, info: dict, cfg: Config,
                    log: logging.Logger) -> int:
    coll_name = info["collection"]
    ensure_project_collection(coll_name, cfg)
    coll = _get_collection(coll_name, cfg)
    coarse, fine, parent_idx = chunk_document(text, cfg.chunk)
    if not coarse:
        return 0
    base = {
        "source_file": fp.relative_to(cfg.require_kb_root()).as_posix(),
        "extra_keys": {
            "project_name": info["project_name"],
            "language": info.get("language", ""),
            "author": info.get("author", ""),
            "year": info.get("year", 0),
            "title": info.get("title", ""),
        },
    }
    log.info(f"    切片完成: {len(coarse)} 粗块 + {len(fine)} 细块")
    return _insert_coarse_then_fine(coll, coarse, fine, parent_idx, base, cfg, log)


def process_fieldwork(fp: Path, text: str, info: dict, cfg: Config,
                      log: logging.Logger) -> int:
    ensure_fieldwork_kb(cfg)
    coll = _get_collection("fieldwork_kb", cfg)
    proj_dir = get_field_project_dir(fp, cfg)
    meta = parse_project_info(proj_dir) if proj_dir else {
        "location": "", "research_date": "", "researchers": "", "notes": ""
    }
    coarse, fine, parent_idx = chunk_document(text, cfg.chunk)
    if not coarse:
        return 0
    base = {
        "source_file": fp.relative_to(cfg.require_kb_root()).as_posix(),
        "extra_keys": {
            "source_type": info["source_type"],
            "source_path": str(fp.parent),
            "project_name": info["project_name"],
            "location": meta["location"],
            "research_date": meta["research_date"],
            "researchers": meta["researchers"],
            "notes": meta["notes"],
        },
    }
    log.info(f"    切片完成: {len(coarse)} 粗块 + {len(fine)} 细块 (类型: {info['source_type']})")
    return _insert_coarse_then_fine(coll, coarse, fine, parent_idx, base, cfg, log)


def _check_and_update_fieldwork_meta(db_conn, proj_dir_map: dict[str, Path],
                                     cfg: Config, log: logging.Logger) -> int:
    FIELDWORK_META_KEYS = ["location", "research_date", "researchers", "notes"]
    coll = _get_collection("fieldwork_kb", cfg)
    updated = 0
    for project_name, proj_dir in proj_dir_map.items():
        info_file = proj_dir / "_项目信息.md"
        if not info_file.exists():
            continue
        current_hash = sha256_file(info_file)
        saved_hash = get_project_meta_hash(db_conn, str(proj_dir))
        if saved_hash == current_hash:
            continue
        meta = parse_project_info(proj_dir)
        expr = f"project_name == '{project_name}'"
        results = coll.query(expr=expr, output_fields=["id"], limit=16384)
        if not results:
            save_project_meta_hash(db_conn, str(proj_dir), current_hash)
            continue
        id_list = [r["id"] for r in results]
        for start in range(0, len(id_list), 30):
            batch_ids = id_list[start:start + 30]
            updated_rows = [
                {"id": mid, **{k: meta[k] for k in FIELDWORK_META_KEYS}}
                for mid in batch_ids
            ]
            coll.upsert(updated_rows, partial_update=True)
        save_project_meta_hash(db_conn, str(proj_dir), current_hash)
        updated += 1
        log.info(f"  ✓ 元数据已更新: {project_name} ({len(id_list)} 条)")
    return updated


def _cleanup_empty_project_collections(db_conn, cfg: Config,
                                       log: logging.Logger) -> int:
    from pymilvus import Collection, utility
    ensure_connected(cfg)
    active_colls = set()
    rows = db_conn.execute(
        "SELECT DISTINCT collection_name FROM file_state WHERE status = 'done'"
    ).fetchall()
    for r in rows:
        active_colls.add(r[0])
    dropped = 0
    for name in utility.list_collections():
        if not name.startswith("proj_"):
            continue
        if name in active_colls:
            continue
        try:
            row_count = Collection(name).num_entities
        except Exception as e:
            log.warning(f"  跳过集合检查 {name}: {e}")
            continue
        if row_count:
            log.info(f"  跳过非空集合 {name}（{row_count} 条，状态库无记录，保留以免误删）")
            continue
        if name in _collection_cache:
            _collection_cache.pop(name)
        utility.drop_collection(name)
        log.info(f"  ✓ 已清理空 Collection: {name}")
        dropped += 1
    return dropped


def run_import(cfg: Config, dry_run: bool = False,
               logger: logging.Logger | None = None) -> dict:
    """增量导入：扫描 -> 对比 hash -> 切片 -> 写入 Milvus。

    dry_run 只读：不写状态库、不调用 Milvus，只报告计划。
    """
    log = logger or logging.getLogger("kbimporter")
    root = cfg.require_kb_root()
    files = scan_all_files(cfg)
    log.info(f"扫描到 {len(files)} 个 .md 文件")

    if dry_run:
        conn = _read_only_state(cfg)
    else:
        conn = init_db(cfg.state_db)

    stats = {"new": 0, "modified": 0, "recovered": 0, "skipped": 0, "empty": 0,
             "error": 0, "deleted": 0}
    try:
        disk_paths = {str(fp) for fp in files}
        all_states = get_all_done_file_states(conn)
        deleted_paths = [s["path"] for s in all_states if s["path"] not in disk_paths]
        if deleted_paths:
            log.info(f"检测到 {len(deleted_paths)} 个已删除文件，清理中...")
            for dp in deleted_paths:
                state = next(s for s in all_states if s["path"] == dp)
                rel_path = _source_rel_path(dp, root)
                if dry_run:
                    log.info(f"  [模拟] 清理 Milvus 数据: {rel_path}")
                else:
                    try:
                        coll = _get_collection(state["collection"], cfg)
                        escaped_rel = rel_path.replace("'", "\\'")
                        coll.delete(f"source_file == '{escaped_rel}'")
                        log.info(f"  ✓ 已清理 Milvus 数据: {rel_path}")
                    except Exception as e:
                        log.info(f"  ✗ 清理失败 ({rel_path}): {e}")
                        stats["error"] += 1
            if dry_run:
                stats["deleted"] = len(deleted_paths)
            else:
                purge_deleted_records(conn, deleted_paths)
                stats["deleted"] = len(deleted_paths)

        fieldwork_proj_dirs: dict[str, Path] = {}
        for i, fp in enumerate(files):
            info = classify_file(fp, cfg)
            if info is None:
                continue
            if info["kb_type"] == "fieldwork":
                proj_dir = get_field_project_dir(fp, cfg)
                if proj_dir and info["project_name"] not in fieldwork_proj_dirs:
                    fieldwork_proj_dirs[info["project_name"]] = proj_dir

            file_hash = sha256_file(fp)
            rel = fp.relative_to(root)
            log.info(f"[{i+1}/{len(files)}] {rel}")
            state = get_file_state(conn, str(fp))
            if state and state["status"] == "done" and state["hash"] == file_hash:
                log.info("    未变化，跳过")
                stats["skipped"] += 1
                continue
            if state and state["status"] == "processing":
                log.info("    上次中断，重新处理")
                stats["recovered"] += 1
            elif state and state["hash"] != file_hash:
                log.info("    文件已修改，重新处理")
                stats["modified"] += 1
            else:
                log.info("    新文件，开始处理")
                stats["new"] += 1

            if dry_run:
                text = read_text_safe(fp)
                if not text.strip():
                    log.info("    [模拟] 空文件，记录状态并跳过")
                    stats["empty"] += 1
                    continue
                coarse, fine, _ = chunk_document(text, cfg.chunk)
                log.info(f"    [模拟] 切片 {len(coarse)} 粗块 + {len(fine)} 细块")
                continue

            mark_processing(conn, str(fp), file_hash, info["collection"])
            _delete_old_vectors(info["collection"], fp.relative_to(root).as_posix(), cfg)
            try:
                text = read_text_safe(fp)
                if not text.strip():
                    log.info("    文件为空，记录状态并跳过")
                    mark_done(conn, str(fp), file_hash, info["collection"], 0)
                    stats["empty"] += 1
                    continue
                if info["kb_type"] == "academic":
                    count = process_academic(fp, text, info, cfg, log)
                elif info["kb_type"] == "project":
                    count = process_project(fp, text, info, cfg, log)
                elif info["kb_type"] == "fieldwork":
                    count = process_fieldwork(fp, text, info, cfg, log)
                else:
                    count = 0
                mark_done(conn, str(fp), file_hash, info["collection"], count)
                log.info(f"    ✓ 处理完成 ({count} 条)")
            except Exception as e:
                log.info(f"    ✗ 处理失败: {e}")
                stats["error"] += 1

        if fieldwork_proj_dirs and not dry_run:
            log.info("检查田野调查项目元数据变更...")
            meta_updated = _check_and_update_fieldwork_meta(
                conn, fieldwork_proj_dirs, cfg, log
            )
            if meta_updated == 0:
                log.info("  所有项目元数据均未变化")

        if not dry_run:
            dropped = _cleanup_empty_project_collections(conn, cfg, log)
            log.info("正在 flush 所有 Collection...")
            from pymilvus import Collection
            for name, coll in _collection_cache.items():
                coll.flush()
                log.info(f"  ✓ {name} 已 flush")
    finally:
        conn.close()

    log.info("=" * 50)
    log.info(f"导入完成: 新增 {stats['new']}, 修改 {stats['modified']}, "
             f"中断恢复 {stats['recovered']}, 跳过 {stats['skipped']}, "
             f"空文件 {stats['empty']}, 失败 {stats['error']}, "
             f"已删除 {stats['deleted']}")
    return stats


def _read_only_state(cfg: Config) -> sqlite3.Connection:
    if cfg.state_db and cfg.state_db.exists():
        return sqlite3.connect(f"file:{cfg.state_db.as_posix()}?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS file_state (
            file_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            collection_name TEXT NOT NULL, chunk_count INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS project_meta_state (
            project_dir TEXT PRIMARY KEY, meta_hash TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn
