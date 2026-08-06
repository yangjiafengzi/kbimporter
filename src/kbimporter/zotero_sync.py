from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

from kbimporter.config import Config
from kbimporter.util import ensure_dir, move_to_trash


def calc_sha256(filepath: str | Path, chunk_size: int = 8192) -> str | None:
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(chunk_size):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
        logging.getLogger("kbimporter").warning(
            f"哈希计算失败: {os.path.basename(filepath)}"
        )
        return None


def load_hash_history(path: str | Path) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, PermissionError):
        logging.getLogger("kbimporter").warning("哈希记录读取异常，初始化新记录")
        return {}


def save_hash_history(path: str | Path, history: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.getLogger("kbimporter").error(f"哈希记录保存失败: {e}")


def get_base_name(filename: str) -> str:
    name, _ = os.path.splitext(filename)
    return name


def extract_text_head(filepath: str | Path, char_limit: int = 500) -> str:
    """提取 PDF/EPUB 开头文本，用于判断语言版本。"""
    fp = str(filepath)
    try:
        if fp.lower().endswith(".pdf"):
            import fitz
            doc = fitz.open(fp)
            text = ""
            for page_num in range(min(5, len(doc))):
                text += doc[page_num].get_text()
                if len(text) >= char_limit:
                    break
            doc.close()
            return text[:char_limit]
        elif fp.lower().endswith(".epub"):
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
            book = epub.read_epub(fp, options={"ignore_ncx": True})
            text = ""
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                content = item.get_content().decode("utf-8", errors="replace")
                soup = BeautifulSoup(content, "html.parser")
                text += soup.get_text()
                if len(text) >= char_limit:
                    break
            return text[:char_limit]
    except Exception:
        return ""
    return ""


def calc_chinese_ratio(text: str) -> float:
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fa5]", text))
    return chinese_chars / len(text)


def sync_zotero(cfg: Config, dry_run: bool = False,
                logger: logging.Logger | None = None) -> dict:
    """同步 Zotero storage 到知识库文献目录。

    行为与原脚本一致：按基础名分组，选择中文比例最低的版本（原文），
    复制到 library；清理过期记录。所有删除操作改为移入回收目录。
    """
    log = logger or logging.getLogger("kbimporter")
    source_dir = cfg.zotero_storage
    library_dir = cfg.library_dir
    hash_file = cfg.hash_history_file
    target_extensions = cfg.target_extensions

    if not source_dir or not source_dir.is_dir():
        log.error(f"源目录不存在: {source_dir}")
        return {"error": "source_dir_missing"}
    if not dry_run:
        ensure_dir(library_dir)

    history = load_hash_history(hash_file)
    log.info(f"已加载 {len(history)} 条哈希记录")

    # 阶段 1：扫描 Zotero 库
    source_files: list[str] = []
    for root, _, files in os.walk(source_dir):
        for f in files:
            if os.path.splitext(f)[1].lower() in target_extensions:
                source_files.append(os.path.join(root, f))
    log.info(f"发现 {len(source_files)} 个目标文件")

    current_records: dict[str, dict] = {}
    for i, src in enumerate(source_files, 1):
        h = calc_sha256(src)
        if h is None:
            continue
        if h in history:
            ratio = history[h]["chinese_ratio"]
        else:
            ratio = calc_chinese_ratio(extract_text_head(src))
        current_records[h] = {"path": src, "chinese_ratio": ratio}
        if i % 50 == 0 or i == len(source_files):
            log.info(f"  已扫描 {i}/{len(source_files)}")

    # 阶段 2：清理过期记录
    stale_hashes = [h for h in history if h not in current_records]
    cleaned = 0
    for h in stale_hashes:
        record = history[h]
        base = get_base_name(os.path.basename(record["path"]))
        was_copied = record.get("copied", False)
        ratio = record.get("chinese_ratio", 0)
        log.info(f"  [过期记录] 哈希={h[:16]}... 文件={os.path.basename(record['path'])} 中文比例={ratio:.2%} 已复制={was_copied}")
        if was_copied:
            for ext in target_extensions:
                fp = library_dir / f"{base}{ext}"
                if fp.exists():
                    dest = move_to_trash(fp, cfg.trash_dir, dry_run=dry_run)
                    log.info(f"             [移入回收] {base}{ext}" + (f" -> {dest}" if dest else ""))
            md_path = library_dir / f"{base}.md"
            if md_path.exists():
                dest = move_to_trash(md_path, cfg.trash_dir, dry_run=dry_run)
                log.info(f"             [移入回收] {base}.md" + (f" -> {dest}" if dest else ""))
        else:
            log.info("             [跳过] 该文件未被复制到目标目录")
        del history[h]
        cleaned += 1

    # 阶段 3：按基础名分组，选择最优版本
    base_name_groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for h, record in current_records.items():
        base = get_base_name(os.path.basename(record["path"]))
        base_name_groups[base].append((h, record))

    copied, skipped, errors = 0, 0, 0
    for base, group in base_name_groups.items():
        log.info(f"  [处理文件组] {base} ({len(group)} 个候选)")
        best_h, best_record = min(group, key=lambda x: x[1]["chinese_ratio"])
        log.info(f"               最优选择: {os.path.basename(best_record['path'])} 中文比例={best_record['chinese_ratio']:.2%}")

        currently_copied_h = None
        for h in history:
            if history[h].get("copied"):
                h_base = get_base_name(os.path.basename(history[h]["path"]))
                if h_base == base:
                    currently_copied_h = h
                    break

        best_filename = os.path.basename(best_record["path"])
        best_base = get_base_name(best_filename)
        best_md_path = library_dir / f"{best_base}.md"
        best_dest_exists = best_md_path.exists()
        need_copy = (currently_copied_h != best_h) or not best_dest_exists

        if need_copy:
            reason = "首次复制" if currently_copied_h is None else "发现更优版本"
            log.info(f"               [需要操作] {reason}")
            dest_path = library_dir / best_filename
            if dry_run:
                log.info(f"               [模拟复制] {best_filename}")
            else:
                try:
                    shutil.copy2(best_record["path"], dest_path)
                    log.info(f"               [复制新文件] {best_filename}")
                except Exception as e:
                    errors += 1
                    log.error(f"  [复制失败] {best_filename}: {e}")
                    continue
            if currently_copied_h:
                history[currently_copied_h]["copied"] = False
            history[best_h] = {
                "path": best_record["path"],
                "chinese_ratio": best_record["chinese_ratio"],
                "copied": True,
            }
            copied += 1
        else:
            history[best_h] = {
                "path": best_record["path"],
                "chinese_ratio": best_record["chinese_ratio"],
                "copied": True,
            }
            skipped += 1
            log.info("               [跳过] 已是最优版本")

    new_records = 0
    for h, record in current_records.items():
        if h not in history:
            history[h] = {
                "path": record["path"],
                "chinese_ratio": record["chinese_ratio"],
                "copied": False,
            }
            new_records += 1

    if not dry_run:
        save_hash_history(hash_file, history)
    else:
        log.info("  [dry-run] 未保存哈希记录")

    stats = {
        "scanned": len(source_files),
        "copied": copied,
        "skipped": skipped,
        "cleaned": cleaned,
        "errors": errors,
        "new_records": new_records,
    }
    log.info("=" * 50)
    log.info(f"同步完成: 新增/替换 {copied}, 跳过 {skipped}, 清理 {cleaned}, 错误 {errors}")
    return stats
