from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from kbimporter.config import Config
from kbimporter.scanner import init_db, get_origin, set_origin
from kbimporter.util import ensure_dir, move_to_trash, sha256_file


def get_chinese_ratio(text: str) -> float:
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return chinese_chars / len(text)


def extract_pdf_text(pdf_path: str | Path, max_pages: int = 3) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages[:max_pages]:
                page_text = page.extract_text()
                if page_text:
                    text += page_text
            return text
    except Exception:
        return ""


def extract_epub_text(epub_path: str | Path, max_chars: int = 5000) -> str:
    try:
        from ebooklib import epub
        from bs4 import BeautifulSoup
        book = epub.read_epub(epub_path)
        text = ""
        for item in book.get_items_of_type(epub.ITEM_DOCUMENT):
            content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(content, "html.parser")
            text += soup.get_text()
            if len(text) >= max_chars:
                break
        return text[:max_chars]
    except Exception:
        return ""


def extract_text(filepath: str | Path) -> str:
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        return extract_pdf_text(filepath)
    elif ext == ".epub":
        return extract_epub_text(filepath)
    return ""


def _walk_supported(root: Path, exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if Path(filename).suffix.lower() in exts:
                files.append(Path(dirpath) / filename)
    return files


def remove_duplicates_by_hash(root: Path, exts: set[str], cfg: Config,
                              dry_run: bool, logger: logging.Logger) -> int:
    log = logger
    log.info("=" * 60)
    log.info("步骤1: 按哈希值删除完全相同的文件")
    hash_to_files: dict[str, list[Path]] = defaultdict(list)
    for fp in _walk_supported(root, exts):
        h = sha256_file(fp)
        if h:
            hash_to_files[h].append(fp)
    log.info(f"扫描到 {sum(len(v) for v in hash_to_files.values())} 个文件")
    delete_count = 0
    for file_hash, file_list in hash_to_files.items():
        if len(file_list) <= 1:
            continue
        log.info(f"发现相同文件 (哈希: {file_hash[:16]}...):")
        for fp in file_list:
            log.info(f"  - {os.path.relpath(fp, root)}")
        keep, *delete_files = file_list
        log.info(f"  -> 保留: {os.path.relpath(keep, root)}")
        for fp in delete_files:
            dest = move_to_trash(fp, cfg.trash_dir, dry_run=dry_run)
            log.info(f"  -> {'[模拟] 移入回收' if dry_run else '已移入回收'}: {os.path.relpath(fp, root)}" + (f" -> {dest}" if dest else ""))
            delete_count += 1
    log.info(f"共发现 {sum(1 for v in hash_to_files.values() if len(v) > 1)} 组相同文件，处理 {delete_count} 个")
    return delete_count


def find_duplicate_groups(root: Path, exts: set[str]) -> dict[tuple[Path, str], dict[int, Path]]:
    pattern = re.compile(r"^(.+)-(\d+)$")
    files_by_group: dict[tuple[Path, str], dict[int, Path]] = {}
    for fp in _walk_supported(root, exts):
        name_without_ext = fp.stem
        match = pattern.match(name_without_ext)
        if match:
            base_name = match.group(1)
            suffix_num = int(match.group(2))
        else:
            base_name = name_without_ext
            suffix_num = 0
        key = (fp.parent, base_name)
        files_by_group.setdefault(key, {})[suffix_num] = fp
    return {key: versions for key, versions in files_by_group.items() if len(versions) > 1}


def process_duplicates(root: Path, exts: set[str], cfg: Config,
                       dry_run: bool, logger: logging.Logger) -> int:
    """同名多版本：保留中文比例最低的版本（原文），其余移入回收。"""
    log = logger
    log.info("\n" + "=" * 60)
    log.info("步骤2: 处理同名不同后缀的文件")
    groups = find_duplicate_groups(root, exts)
    if not groups:
        log.info("未找到同名不同后缀的文件。")
        return 0
    log.info(f"找到 {len(groups)} 组同名不同后缀文件")
    processed = 0
    for (dirpath, base_name), versions in groups.items():
        version_info: dict[int, dict] = {}
        for suffix_num, fp in versions.items():
            ratio = get_chinese_ratio(extract_text(fp))
            version_info[suffix_num] = {"path": fp, "chinese_ratio": ratio, "ext": fp.suffix}
            label = f"{base_name}{fp.suffix}" if suffix_num == 0 else f"{base_name}-{suffix_num}{fp.suffix}"
            log.info(f"  - {label} (中文比例: {ratio:.1%})")
        min_ratio = min(info["chinese_ratio"] for info in version_info.values())
        candidates = [s for s, info in version_info.items() if info["chinese_ratio"] == min_ratio]
        keep_suffix = 0 if 0 in candidates else min(candidates)
        keep_path = version_info[keep_suffix]["path"]
        keep_ext = version_info[keep_suffix]["ext"]
        log.info(f"  -> 保留: {base_name}{keep_ext} (中文比例最低)")
        for suffix_num, info in version_info.items():
            if suffix_num == keep_suffix:
                continue
            dest = move_to_trash(info["path"], cfg.trash_dir, dry_run=dry_run)
            label = f"{base_name}{info['ext']}" if suffix_num == 0 else f"{base_name}-{suffix_num}{info['ext']}"
            log.info(f"  -> {'[模拟] 移入回收' if dry_run else '已移入回收'}: {label}" + (f" -> {dest}" if dest else ""))
            processed += 1
        if keep_suffix != 0:
            new_path = dirpath / f"{base_name}{keep_ext}"
            if new_path.exists():
                log.info(f"  -> 跳过重命名，目标已存在: {new_path.name}")
            elif dry_run:
                log.info(f"  -> [模拟] 重命名: {base_name}-{keep_suffix}{keep_ext} -> {base_name}{keep_ext}")
                processed += 1
            else:
                os.rename(keep_path, new_path)
                log.info(f"  -> 已重命名: {base_name}-{keep_suffix}{keep_ext} -> {base_name}{keep_ext}")
                processed += 1
    log.info(f"步骤2 完成，处理 {processed} 项")
    return processed


def rename_files_with_suffix(root: Path, exts: set[str], cfg: Config,
                             dry_run: bool, logger: logging.Logger) -> int:
    """把仍带 -N 后缀的文献文件重命名为基础名。"""
    log = logger
    log.info("\n" + "=" * 60)
    log.info("步骤3: 重命名带 -数字 后缀的文件")
    pattern = re.compile(r"^(.+)-(\d+)$")
    rename_count = 0
    for fp in _walk_supported(root, exts):
        match = pattern.match(fp.stem)
        if not match:
            continue
        base_name = match.group(1)
        new_path = fp.parent / f"{base_name}{fp.suffix}"
        if new_path.exists():
            log.info(f"跳过: {fp.name} (目标文件已存在: {new_path.name})")
            continue
        log.info(f"重命名: {fp.name} -> {new_path.name}")
        if dry_run:
            log.info("  -> [模拟] 重命名")
            rename_count += 1
        else:
            try:
                os.rename(fp, new_path)
                log.info("  -> 已重命名")
                rename_count += 1
            except Exception as e:
                log.warning(f"  -> 重命名失败: {e}")
    log.info(f"共重命名 {rename_count} 个文件")
    return rename_count


def _open_origin_conn(cfg: Config, dry_run: bool) -> sqlite3.Connection:
    if not dry_run:
        return init_db(cfg.state_db)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS file_origin (
            file_path TEXT PRIMARY KEY, origin TEXT NOT NULL,
            file_hash TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    if cfg.state_db and cfg.state_db.exists():
        try:
            src = sqlite3.connect(f"file:{cfg.state_db.as_posix()}?mode=ro", uri=True)
            rows = src.execute(
                "SELECT file_path, origin, file_hash FROM file_origin"
            ).fetchall()
            conn.executemany(
                "INSERT OR REPLACE INTO file_origin (file_path, origin, file_hash) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
            src.close()
        except sqlite3.Error:
            pass
    return conn


def copy_md_from_zotero(project_root: Path, zotero_dir: Path, cfg: Config,
                        conn: sqlite3.Connection, dry_run: bool,
                        logger: logging.Logger) -> dict:
    """从 Zotero 库复制同名 MD 到项目目录，并按策略替换旧 OCR MD。"""
    log = logger
    log.info("\n" + "=" * 60)
    log.info("步骤4: 从Zotero库复制同名md文件")
    if not zotero_dir.exists():
        log.info(f"Zotero库目录不存在: {zotero_dir}")
        return {"copied": 0, "replaced": 0, "skipped": 0}

    project_files: dict[str, Path] = {}
    for fp in _walk_supported(project_root, cfg.supported_extensions):
        project_files[fp.stem] = fp
    zotero_md_files: dict[str, Path] = {}
    if zotero_dir.exists():
        for fp in zotero_dir.rglob("*.md"):
            zotero_md_files[fp.stem] = fp
    log.info(f"项目目录中有 {len(project_files)} 个文献文件，Zotero库中有 {len(zotero_md_files)} 个md文件")

    copied = replaced = skipped = 0
    for name, project_path in project_files.items():
        if name not in zotero_md_files:
            continue
        zotero_md = zotero_md_files[name]
        target_path = project_path.parent / f"{name}.md"
        if not target_path.exists():
            log.info(f"找到同名文件: {name}")
            if dry_run:
                log.info(f"  -> [模拟] 复制: {zotero_md} -> {target_path}")
                copied += 1
            else:
                ensure_dir(target_path.parent)
                shutil.copy2(zotero_md, target_path)
                set_origin(conn, str(target_path), "zotero_md", sha256_file(target_path))
                log.info(f"  -> 已复制: {target_path}")
                copied += 1
            continue

        existing_hash = sha256_file(target_path)
        library_hash = sha256_file(zotero_md)
        if existing_hash == library_hash:
            skipped += 1
            continue
        origin = get_origin(conn, str(target_path))
        should_replace = False
        reason = ""
        if cfg.replace_existing_md == "always":
            should_replace = True
            reason = "配置为 always"
        elif cfg.replace_existing_md == "never":
            reason = "配置为 never"
        elif origin == "ocr_md":
            should_replace = True
            reason = "现有 MD 是本程序生成的 OCR 产物"
        else:
            reason = f"现有 MD 来源为 {origin or '未知'}，按策略跳过（可用 --replace-existing 强制）"
        if should_replace:
            dest = move_to_trash(target_path, cfg.trash_dir, dry_run=dry_run)
            log.info(f"  -> 替换现有 MD: {target_path.name}" + (f" (旧文件 -> {dest})" if dest else ""))
            if not dry_run:
                shutil.copy2(zotero_md, target_path)
                set_origin(conn, str(target_path), "zotero_md", sha256_file(target_path))
            replaced += 1
        else:
            log.info(f"  -> 跳过替换: {target_path.name}（{reason}）")
            skipped += 1
    log.info(f"步骤4 完成: 复制 {copied}, 替换 {replaced}, 跳过 {skipped}")
    return {"copied": copied, "replaced": replaced, "skipped": skipped}


def find_pdf_md_pairs(root: Path) -> list[tuple[Path, str, Path, Path]]:
    pairs: list[tuple[Path, str, Path, Path]] = []
    for dirpath, _, filenames in os.walk(root):
        pdf_files = {f[:-4] for f in filenames if f.lower().endswith(".pdf")}
        md_files = {f[:-3] for f in filenames if f.lower().endswith(".md")}
        for name in pdf_files & md_files:
            pairs.append((Path(dirpath), name, Path(dirpath) / f"{name}.pdf", Path(dirpath) / f"{name}.md"))
    return pairs


def process_pdf_md_pairs(root: Path, cfg: Config, dry_run: bool,
                         logger: logging.Logger) -> int:
    """同名 PDF/MD 保留 MD，PDF 移入回收目录。"""
    log = logger
    log.info("\n" + "=" * 60)
    log.info("步骤5: 处理同名 .pdf 和 .md 文件")
    pairs = find_pdf_md_pairs(root)
    if not pairs:
        log.info("未找到同名的 .pdf 和 .md 文件。")
        return 0
    log.info(f"找到 {len(pairs)} 组同名的 .pdf 和 .md 文件")
    processed = 0
    for dirpath, name, pdf_path, md_path in pairs:
        dest = move_to_trash(pdf_path, cfg.trash_dir, dry_run=dry_run)
        log.info(f"{name}: {'[模拟] 移入回收' if dry_run else '已移入回收'} {pdf_path.name} (保留 .md)" + (f" -> {dest}" if dest else ""))
        processed += 1
    return processed


def run_dedupe(cfg: Config, dry_run: bool = True, scope: str = "all",
               logger: logging.Logger | None = None) -> dict:
    """统一去重清理：项目文献 5 步 + Zotero 文献库同名清理。"""
    log = logger or logging.getLogger("kbimporter")
    mode = "模拟运行" if dry_run else "实际执行"
    log.info("=" * 60)
    log.info(f"同名不同后缀文件筛选工具 ({mode})")
    log.info(f"项目目录: {cfg.project_root}")
    log.info(f"Zotero库: {cfg.library_dir}")
    log.info("=" * 60)

    conn = _open_origin_conn(cfg, dry_run)
    stats: dict = {}
    try:
        if scope in ("project", "all") and cfg.project_root and cfg.project_root.exists():
            stats["hash_dedupe"] = remove_duplicates_by_hash(
                cfg.project_root, cfg.supported_extensions, cfg, dry_run, log
            )
            stats["version_dedupe"] = process_duplicates(
                cfg.project_root, cfg.supported_extensions, cfg, dry_run, log
            )
            stats["rename"] = rename_files_with_suffix(
                cfg.project_root, cfg.supported_extensions, cfg, dry_run, log
            )
            md_stats = copy_md_from_zotero(
                cfg.project_root, cfg.library_dir, cfg, conn, dry_run, log
            )
            stats.update(md_stats)
            stats["pdf_md_cleanup"] = process_pdf_md_pairs(
                cfg.project_root, cfg, dry_run, log
            )
        if scope in ("library", "all") and cfg.library_dir and cfg.library_dir.exists():
            log.info("\n" + "=" * 60)
            log.info("Zotero文献库: 处理同名 .pdf 和 .md")
            stats["library_pdf_md_cleanup"] = process_pdf_md_pairs(
                cfg.library_dir, cfg, dry_run, log
            )
    finally:
        conn.close()
    return stats
