from __future__ import annotations

import hashlib
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


def sha256_file(file_path: str | Path, chunk_size: int = 8192) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text_safe(file_path: str | Path) -> str:
    """按多种编码读取文本文件，尽量兼容历史文件。"""
    fp = Path(file_path)
    for enc in ("utf-8", "utf-8-sig", "utf-16", "gbk", "gb18030", "latin-1"):
        try:
            return fp.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return fp.read_bytes().decode("utf-8", errors="replace")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def setup_logging(log_file: str | Path | None = None,
                  level: int = logging.INFO) -> logging.Logger:
    """配置统一日志：控制台 + 可选文件。"""
    logger = logging.getLogger("kbimporter")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        ensure_dir(Path(log_file).parent)
        fh = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    counter = 1
    while True:
        candidate = dest.with_name(f"{dest.stem}_{counter}{dest.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def plan_trash_dest(src: str | Path, trash_dir: str | Path) -> Path:
    """规划回收位置：<trash_dir>/<批次时间戳>/<原文件名>。"""
    src_p = Path(src)
    batch = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(trash_dir) / batch / src_p.name


def move_to_trash(src: str | Path, trash_dir: str | Path,
                  dry_run: bool = False) -> Path | None:
    """把文件移入回收目录（不直接删除）。dry_run 时只返回目标路径。"""
    src_p = Path(src)
    if not src_p.exists():
        return None
    dest = _unique_dest(plan_trash_dest(src_p, trash_dir))
    if dry_run:
        return dest
    ensure_dir(dest.parent)
    shutil.move(str(src_p), str(dest))
    return dest


def trash_many(paths: Iterable[str | Path], trash_dir: str | Path,
               dry_run: bool = False, label: str = "") -> list[Path]:
    moved: list[Path] = []
    for p in paths:
        dest = move_to_trash(p, trash_dir, dry_run=dry_run)
        if dest:
            moved.append(dest)
    return moved


def remove_file(file_path: str | Path):
    """直接删除（仅用于明确要求 delete 模式时）。"""
    Path(file_path).unlink(missing_ok=True)
