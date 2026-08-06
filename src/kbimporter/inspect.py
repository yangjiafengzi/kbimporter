from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from kbimporter.config import Config
from kbimporter.util import sha256_file


def _state_summary(db: Path) -> dict:
    info: dict = {"exists": False}
    if not db.exists():
        return info
    info["exists"] = True
    info["size"] = db.stat().st_size
    info["sha256"] = sha256_file(db)
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        files = chunks = meta = 0
        if "file_state" in tables:
            files = conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0]
            chunks = conn.execute(
                "SELECT COALESCE(SUM(chunk_count), 0) FROM file_state"
            ).fetchone()[0]
        if "project_meta_state" in tables:
            meta = conn.execute(
                "SELECT COUNT(*) FROM project_meta_state"
            ).fetchone()[0]
        conn.close()
        info.update(tables=tables, files=files, chunks=chunks, meta=meta)
    except sqlite3.Error as e:
        info["error"] = str(e)
    return info


def scan_state_files(cfg: Config) -> dict:
    """扫描候选状态文件：配置指向、旧版 0向量化、程序默认位置。"""
    candidates: dict[str, Path] = {}
    if cfg.state_db:
        candidates["configured"] = cfg.state_db
    root = cfg.kb_root
    if root:
        legacy = root / "0向量化" / "import_state.db"
        default = root / ".kb" / "state.db"
        if legacy not in candidates.values():
            candidates["legacy_import_state_db"] = legacy
        if default not in candidates.values():
            candidates["default_kb_state_db"] = default
    result = {}
    for label, path in candidates.items():
        summary = _state_summary(path)
        summary["path"] = str(path)
        result[label] = summary
    return result


def scan_milvus(cfg: Config) -> dict:
    """只读扫描 Milvus：可用性 + 现有集合 + 行数。"""
    try:
        from pymilvus import MilvusClient
    except ImportError:
        return {"available": False, "reason": "pymilvus 未安装（pip install kbimporter[import]）"}
    try:
        client = MilvusClient(uri=f"http://{cfg.milvus.host}:{cfg.milvus.port}", timeout=5)
        names = client.list_collections()
        collections = []
        for name in sorted(names):
            row_count = None
            try:
                stats = client.get_collection_stats(name)
                row_count = (stats or {}).get("row_count")
            except Exception:
                row_count = None
            kind = "project" if name.startswith("proj_") else name
            collections.append({"name": name, "kind": kind, "row_count": row_count})
        client.close()
        return {"available": True, "collections": collections}
    except Exception as e:
        try:
            client.close()
        except Exception:
            pass
        return {"available": False, "reason": str(e)}


def state_collections(cfg: Config) -> dict:
    """只读读取状态库引用的集合与文件/切片统计。"""
    db = cfg.state_db
    if not db or not db.exists():
        return {"exists": False, "collections": {}}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        rows = conn.execute(
            """SELECT collection_name, COUNT(*), COALESCE(SUM(chunk_count), 0)
               FROM file_state WHERE status = 'done'
               GROUP BY collection_name"""
        ).fetchall()
        conn.close()
        return {
            "exists": True,
            "collections": {
                r[0]: {"files": r[1], "chunks": r[2]} for r in rows
            },
        }
    except sqlite3.Error as e:
        return {"exists": False, "error": str(e), "collections": {}}


def run_scan(cfg: Config, state_only: bool = False, milvus_only: bool = False,
             logger: logging.Logger | None = None) -> dict:
    log = logger or logging.getLogger("kbimporter")
    result: dict = {}
    if not milvus_only:
        log.info("=" * 60)
        log.info("状态文件扫描（只读）")
        state = scan_state_files(cfg)
        result["state"] = state
        for label, info in state.items():
            if not info["exists"]:
                log.info(f"  {label}: 不存在 ({info['path']})")
                continue
            log.info(f"  {label}: {info['path']}")
            log.info(f"    大小={info['size']} 文件状态={info.get('files', '?')} "
                     f"切片={info.get('chunks', '?')} 项目元数据={info.get('meta', '?')}")
            if "tables" in info:
                log.info(f"    表={info['tables']}")
            if "error" in info:
                log.info(f"    读取错误={info['error']}")
    if not state_only:
        log.info("=" * 60)
        log.info("Milvus 库扫描（只读）")
        milvus = scan_milvus(cfg)
        result["milvus"] = milvus
        if not milvus["available"]:
            log.info(f"  Milvus 不可用: {milvus.get('reason')}")
        else:
            collections = milvus["collections"]
            if not collections:
                log.info("  未发现任何集合（首次导入时会自动创建）")
            for c in collections:
                log.info(f"  {c['name']} (kind={c['kind']}, row_count={c['row_count']})")
            refs = state_collections(cfg)
            if refs["exists"] and refs["collections"]:
                existing = {c["name"] for c in collections}
                log.info("-" * 60)
                log.info("状态库引用 vs Milvus 实际集合:")
                for name, stat in sorted(refs["collections"].items()):
                    if name in existing:
                        log.info(f"  ✓ {name}: 状态 {stat['files']} 文件 / {stat['chunks']} 切片")
                    else:
                        log.warning(f"  ✗ {name}: 状态库引用但 Milvus 中不存在 "
                                    f"({stat['files']} 文件 / {stat['chunks']} 切片)——"
                                    f"导入时若直接跳过会造成空集合，请先人工确认")
    return result
