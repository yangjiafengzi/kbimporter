from __future__ import annotations

import re
import logging
import sqlite3
from pathlib import Path

from kbimporter.config import Config
from kbimporter.util import sha256_file


def to_pinyin(text: str) -> str:
    """中文目录名转拼音，用于动态生成 Milvus 集合名。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        raise RuntimeError(
            "缺少依赖 pypinyin：请安装 kbimporter[import] 或 pip install pypinyin"
        )
    return "".join(lazy_pinyin(text))


def detect_language(filename: str) -> str:
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", filename))
    en_count = len(re.findall(r"[a-zA-Z]", filename))
    return "zh" if zh_count >= en_count else "en"


_ACADEMIC_FILENAME_RE = re.compile(
    r"^(?P<author>.+?)\s*[-–—]\s*(?P<year>\d{4})\s*[-–—]\s*(?P<title>.+?)(?:\.md)?$",
    re.IGNORECASE,
)


def parse_academic_filename(filename: str) -> dict:
    m = _ACADEMIC_FILENAME_RE.match(filename.strip())
    if m:
        return {
            "author": m.group("author").strip(),
            "year": int(m.group("year")),
            "title": m.group("title").strip(),
        }
    return {"author": "", "year": 0, "title": filename.rsplit(".md", 1)[0].strip()}


def init_db(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    is_new = not db_path.exists()
    if is_new:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    if is_new:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_state (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'done',
                last_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                collection_name TEXT NOT NULL,
                chunk_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_meta_state (
                project_dir TEXT PRIMARY KEY,
                meta_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_origin (
                file_path TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    return conn


def get_file_state(conn: sqlite3.Connection, file_path: str) -> dict | None:
    row = conn.execute(
        "SELECT file_hash, status FROM file_state WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    if row is None:
        return None
    return {"hash": row[0], "status": row[1]}


def mark_processing(conn: sqlite3.Connection, file_path: str, file_hash: str,
                    collection_name: str):
    conn.execute(
        """INSERT OR REPLACE INTO file_state
           (file_path, file_hash, status, last_processed, collection_name, chunk_count)
           VALUES (?, ?, 'processing', datetime('now'), ?, 0)""",
        (file_path, file_hash, collection_name),
    )
    conn.commit()


def mark_done(conn: sqlite3.Connection, file_path: str, file_hash: str,
              collection_name: str, chunk_count: int):
    conn.execute(
        """UPDATE file_state
           SET status='done', file_hash=?, last_processed=datetime('now'), chunk_count=?
           WHERE file_path=?""",
        (file_hash, chunk_count, file_path),
    )
    conn.commit()


def purge_deleted_records(conn: sqlite3.Connection, file_paths: list[str]):
    for fp in file_paths:
        conn.execute("DELETE FROM file_state WHERE file_path=?", (fp,))
    conn.commit()


def get_all_done_file_states(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT file_path, file_hash, status, collection_name FROM file_state"
    ).fetchall()
    return [{"path": r[0], "hash": r[1], "status": r[2], "collection": r[3]} for r in rows]


def get_project_meta_hash(conn: sqlite3.Connection, project_dir: str) -> str | None:
    row = conn.execute(
        "SELECT meta_hash FROM project_meta_state WHERE project_dir = ?",
        (project_dir,),
    ).fetchone()
    return row[0] if row else None


def save_project_meta_hash(conn: sqlite3.Connection, project_dir: str, meta_hash: str):
    conn.execute(
        """INSERT OR REPLACE INTO project_meta_state (project_dir, meta_hash)
           VALUES (?, ?)""",
        (project_dir, meta_hash),
    )
    conn.commit()


def get_origin(conn: sqlite3.Connection, file_path: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT origin FROM file_origin WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        # 复用旧状态库时可能没有 file_origin 表，按未知来源处理
        return None


def set_origin(conn: sqlite3.Connection, file_path: str, origin: str,
               file_hash: str):
    try:
        conn.execute(
            """INSERT OR REPLACE INTO file_origin (file_path, origin, file_hash, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (file_path, origin, file_hash),
        )
        conn.commit()
    except sqlite3.OperationalError:
        logging.getLogger("kbimporter").warning(
            "file_origin 表不存在，跳过来源记录（旧状态库复用模式）"
        )


def classify_file(fp: Path, cfg: Config) -> dict | None:
    lib_path = cfg.library_dir
    proj_root = cfg.project_root
    field_root = cfg.fieldwork_root

    if fp.suffix.lower() != ".md":
        return None

    if lib_path and (fp.parent == lib_path or lib_path in fp.parents):
        if fp.name.startswith("_"):
            return None
        meta = parse_academic_filename(fp.name)
        return {
            "kb_type": "academic",
            "collection": "academic_library",
            "author": meta["author"],
            "year": meta["year"],
            "title": meta["title"],
        }

    if proj_root and proj_root in fp.parents and fp.parent != proj_root:
        rel = fp.relative_to(proj_root)
        proj_name = rel.parts[0]
        meta = parse_academic_filename(fp.name)
        return {
            "kb_type": "project",
            "collection": "proj_" + to_pinyin(proj_name),
            "project_name": proj_name,
            "language": detect_language(fp.name),
            "author": meta["author"],
            "year": meta["year"],
            "title": meta["title"],
        }

    if field_root and field_root in fp.parents and fp.parent != field_root:
        rel = fp.relative_to(field_root)
        parts = rel.parts
        if len(parts) < 2:
            return None
        proj_part = parts[0]
        source_type = None
        if "笔记" in parts:
            source_type = "note"
        elif "其他材料" in parts:
            source_type = "supplement"
        if source_type is None:
            return None
        return {
            "kb_type": "fieldwork",
            "collection": "fieldwork_kb",
            "source_type": source_type,
            "project_name": proj_part,
        }

    return None


def parse_project_info(project_dir: Path) -> dict:
    info_file = project_dir / "_项目信息.md"
    result = {"location": "", "research_date": "", "researchers": "", "notes": ""}
    if not info_file.exists():
        return result
    from kbimporter.util import read_text_safe
    text = read_text_safe(info_file)
    text = text.lstrip("\ufeff")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- 调研地点：") or line.startswith("- 调研地点:"):
            result["location"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("- 调研时间：") or line.startswith("- 调研时间:"):
            result["research_date"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("- 调研人员：") or line.startswith("- 调研人员:"):
            result["researchers"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    notes_match = re.search(r"## 备注\s*\n(.+)", text, re.DOTALL)
    if notes_match:
        result["notes"] = notes_match.group(1).strip()[:2048]
    return result


def scan_all_files(cfg: Config) -> list[Path]:
    files: list[Path] = []
    for scan_dir in (cfg.library_dir, cfg.project_root, cfg.fieldwork_root):
        if scan_dir and scan_dir.exists():
            files.extend(scan_dir.rglob("*.md"))
    return files


def get_field_project_dir(fp: Path, cfg: Config) -> Path | None:
    field_root = cfg.fieldwork_root
    if field_root and field_root in fp.parents:
        rel = fp.relative_to(field_root)
        if rel.parts:
            return field_root / rel.parts[0]
    return None
