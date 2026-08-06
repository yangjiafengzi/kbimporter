from __future__ import annotations

import logging
import math
import os
import shutil
import stat
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from kbimporter.cloud_ocr import write_cloud_ocr_md
from kbimporter.config import Config
from kbimporter.scanner import init_db, set_origin
from kbimporter.util import ensure_dir, move_to_trash, remove_file, sha256_file


def kill_process_tree(pid: int):
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def _record_ocr_origin(cfg: Config, md_path: Path):
    """把转换产物记录为 ocr_md 来源，供 dedupe 的 ocr_only 替换策略识别。"""
    try:
        conn = init_db(cfg.state_db)
        try:
            set_origin(conn, str(md_path), "ocr_md", sha256_file(md_path))
        finally:
            conn.close()
    except Exception:
        logging.getLogger("kbimporter").warning(
            f"记录 OCR 来源失败（不影响转换结果）: {md_path}"
        )


def _force_rmtree(path: Path):
    """删除目录树；Windows 上先清除只读属性再重试。"""

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(str(path), onerror=onerror)
    except Exception:
        try:
            os.rmdir(path)
        except Exception:
            pass


def _resolve_exe(cmd: str, *env_hints: str) -> str:
    """解析外部引擎可执行文件：绝对路径直接返回，否则查 PATH，再查 conda env。"""
    if os.path.isabs(cmd) or "/" in cmd or "\\" in cmd:
        return cmd
    found = shutil.which(cmd)
    if found:
        return found
    from kbimporter.doctor import _env_exe
    for env in env_hints:
        resolved = _env_exe(env, cmd)
        if resolved:
            return resolved
    return cmd


def run_with_kill(cmd: list[str], timeout: int) -> int:
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        proc.wait(timeout=timeout)
        return proc.returncode
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        raise
    except Exception:
        kill_process_tree(proc.pid)
        raise


def find_files(scan_dir: Path, cfg: Config) -> tuple[list[Path], list[Path]]:
    """扫描待转换文件。已存在同名 .md 的文件默认跳过，避免重复 OCR。"""
    skip_dirs = {d.lower() for d in cfg.skip_dirs}
    pdfs: list[Path] = []
    markitdown_files: list[Path] = []
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        for f in files:
            fpath = Path(root) / f
            ext = fpath.suffix.lower()
            if cfg.skip_existing_md and (fpath.parent / f"{fpath.stem}.md").exists():
                continue
            if ext == ".pdf":
                pdfs.append(fpath)
            elif ext not in cfg.skip_exts and ext in cfg.markitdown_exts:
                markitdown_files.append(fpath)
    pdfs.sort()
    markitdown_files.sort()
    return pdfs, markitdown_files


def build_marker_cmd(input_dir: Path, output_dir: Path, cfg: Config) -> list[str]:
    cmd = [
        _resolve_exe(cfg.marker_cmd, "ocr_env"), str(input_dir),
        "--output_dir", str(output_dir),
        "--output_format", "markdown",
        "--workers", str(cfg.marker_workers),
        "--debug_print",
    ]
    if cfg.enable_llm:
        cmd += [
            "--use_llm",
            "--llm_service", "marker.services.openai.OpenAIService",
            "--OpenAIService_openai_base_url", cfg.llm_base_url,
            "--OpenAIService_openai_api_key", cfg.llm_api_key,
            "--OpenAIService_openai_model", cfg.llm_model,
        ]
    return cmd


def _dispose_original(fpath: Path, cfg: Config, dry_run: bool,
                      log: logging.Logger):
    """转换成功后按策略处理原文件：trash（默认）/ delete / keep。"""
    if cfg.after_convert == "keep":
        return
    if cfg.after_convert == "delete":
        if not dry_run:
            remove_file(fpath)
            log.debug(f"  删除原文件: {fpath}")
        return
    dest = move_to_trash(fpath, cfg.trash_dir, dry_run=dry_run)
    log.debug(f"  移入回收: {fpath}" + (f" -> {dest}" if dest else ""))


def process_markitdown(files: list[Path], scan_dir: Path, cfg: Config,
                       dry_run: bool, failed_files: list[str],
                       log: logging.Logger) -> int:
    if not files:
        log.info("没有需要 MarkItDown 处理的文件")
        return 0
    log.info("=" * 50)
    log.info(f"[阶段1] MarkItDown: 处理 {len(files)} 个非PDF文件")
    success = 0
    for idx, fpath in enumerate(files, 1):
        rel_path = fpath.relative_to(scan_dir)
        base_name = fpath.stem
        tmp_md = cfg.ocr_work_dir / f"md_{idx}" / f"{base_name}.md"
        dest_md = fpath.parent / f"{base_name}.md"
        log.info(f"  [{idx}/{len(files)}] {rel_path}")
        if dry_run:
            log.info(f"  -> [模拟] markitdown {fpath.name} -> {dest_md}")
            success += 1
            continue
        cmd = [_resolve_exe(cfg.markitdown_cmd), str(fpath), "-o", str(tmp_md)]
        log.debug(f"  执行: {' '.join(cmd)}")
        try:
            ensure_dir(tmp_md.parent)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                err = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown"
                log.warning(f"  [{idx}/{len(files)}] 转换失败: {err}")
                failed_files.append(str(rel_path))
                continue
        except subprocess.TimeoutExpired:
            log.warning(f"  [{idx}/{len(files)}] 超时, 跳过")
            failed_files.append(str(rel_path))
            continue
        except Exception as e:
            log.error(f"  [{idx}/{len(files)}] 异常: {e}")
            failed_files.append(str(rel_path))
            continue
        if not tmp_md.is_file():
            log.warning(f"  [{idx}/{len(files)}] 未生成md文件, 跳过")
            failed_files.append(str(rel_path))
            continue
        try:
            shutil.move(str(tmp_md), str(dest_md))
            _record_ocr_origin(cfg, dest_md)
        except Exception as e:
            log.warning(f"  [{idx}/{len(files)}] 移动失败: {e}")
            failed_files.append(str(rel_path))
            continue
        _dispose_original(fpath, cfg, dry_run=False, log=log)
        success += 1
    log.info(f"[阶段1] MarkItDown 完成: {success}/{len(files)} 成功")
    return success


def collect_batch_results(batch_input: Path, batch_output: Path, pdf_dir: Path,
                          dir_rel: Path, cfg: Config, failed_files: list[str],
                          lost_dir: Path, log: logging.Logger) -> int:
    success = 0
    for fpath in batch_input.iterdir():
        if fpath.suffix.lower() != ".pdf":
            continue
        base_name = fpath.stem
        md_path = batch_output / base_name / f"{base_name}.md"
        if md_path.is_file():
            dest_md = pdf_dir / f"{base_name}.md"
            try:
                shutil.move(str(md_path), str(dest_md))
                _record_ocr_origin(cfg, dest_md)
                success += 1
                _dispose_original(fpath, cfg, dry_run=False, log=log)
            except Exception as e:
                log.warning(f"    移动md失败: {base_name}.md, 原因: {e}")
                failed_files.append(str(dir_rel / fpath.name))
                lost_dest = lost_dir / dir_rel / fpath.name
                lost_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(fpath), str(lost_dest))
        else:
            log.warning(f"    未找到输出: {base_name}.md, 移入lost/{dir_rel}")
            failed_files.append(str(dir_rel / fpath.name))
            lost_dest = lost_dir / dir_rel / fpath.name
            lost_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(fpath), str(lost_dest))
    for item in batch_output.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(str(item))
            elif item.is_file():
                item.unlink()
        except Exception:
            pass
    return success


def _run_mineru_single(pdf_path: Path, cfg: Config,
                       log: logging.Logger) -> Path | None:
    """用 MinerU 转换单个 PDF，返回生成的 .md 路径。"""
    out_dir = cfg.ocr_work_dir / "mineru_out"
    ensure_dir(out_dir)
    env = os.environ.copy()
    env["MINERU_MODEL_SOURCE"] = cfg.mineru_model_source
    cmd = [
        _resolve_exe(cfg.mineru_cmd, "mineru_env"), "-p", str(pdf_path),
        "-o", str(out_dir),
        "-b", cfg.mineru_backend,
        "-m", cfg.mineru_method,
    ]
    log.debug(f"  执行: {' '.join(cmd)}")
    try:
        returncode = run_with_kill(cmd, timeout=cfg.timeout_retry_pdf)
    except subprocess.TimeoutExpired:
        log.warning(f"    mineru 超时: {pdf_path.name}")
        return None
    except Exception as e:
        log.warning(f"    mineru 异常: {pdf_path.name}, {e}")
        return None
    if returncode != 0:
        log.warning(f"    mineru 失败 (退出码 {returncode}): {pdf_path.name}")
        return None
    candidates = list(out_dir.rglob("*.md"))
    if not candidates:
        log.warning(f"    mineru 未生成 md: {pdf_path.name}")
        return None
    stem = pdf_path.stem
    preferred = [p for p in candidates if p.parent.name == "auto" and stem in p.name]
    chosen = preferred[0] if preferred else max(candidates, key=lambda p: p.stat().st_size)
    return chosen


def process_retry_engines(lost_dir: Path, scan_dir: Path, cfg: Config,
                          dry_run: bool, failed_files: list[str],
                          log: logging.Logger,
                          pending_pdfs: list[Path] | None = None,
                          skip_files: set[Path] | None = None) -> int:
    """按配置的引擎顺序重试失败的 PDF：marker_single -> mineru -> cloud。"""
    if pending_pdfs is not None:
        items: list[tuple[Path, Path]] = [
            (pdf.parent.relative_to(scan_dir), pdf) for pdf in pending_pdfs
        ]
        log.info("=" * 50)
        log.info(f"[阶段3] 直接引擎处理: {len(items)} 个 PDF（未启用 marker 批处理）")
    else:
        lost_pdfs = [f for f in lost_dir.rglob("*.pdf") if f.parent != lost_dir]
        if skip_files:
            lost_pdfs = [f for f in lost_pdfs if f not in skip_files]
        if not lost_pdfs:
            return 0
        items = []
        for pdf_path in lost_pdfs:
            rel_from_lost = pdf_path.relative_to(lost_dir)
            items.append((rel_from_lost.parent, pdf_path))
        log.info("=" * 50)
        log.info(f"[阶段3] 重试失败PDF: {len(items)} 个（引擎顺序: {' -> '.join(cfg.engines)}）")

    if dry_run:
        for dir_rel, pdf_path in items:
            log.info(f"  -> [模拟] {pdf_path.name}: " + " -> ".join(cfg.engines))
            if "cloud" in cfg.engines and cfg.cloud_ocr.enabled:
                try:
                    from kbimporter.cloud_ocr import ocr_pdf_cloud
                    ocr_pdf_cloud(cfg, pdf_path, dry_run=True, logger=log)
                except Exception as e:
                    log.warning(f"    云端预演失败（{e}），将按通用流程处理")
        return len(items)

    success = 0
    for idx, (dir_rel, pdf_path) in enumerate(items, 1):
        original_dir = scan_dir / dir_rel
        base_name = pdf_path.stem
        dest_md = original_dir / f"{base_name}.md"
        produced_md: Path | None = None
        cleanup_dirs: list[Path] = []
        log.info(f"  [{idx}/{len(items)}] {pdf_path.name}")
        for engine in cfg.engines:
            if engine == "marker":
                # marker 批处理已失败；此处用 marker_single 单文件重试
                if pending_pdfs is not None:
                    continue
                tmp_out = lost_dir / f"retry_{idx}"
                tmp_out.mkdir(parents=True, exist_ok=True)
                cleanup_dirs.append(tmp_out)
                cmd = [
                    _resolve_exe(cfg.marker_single_cmd, "ocr_env"), str(pdf_path),
                    "--output_dir", str(tmp_out),
                    "--output_format", "markdown",
                ]
                try:
                    run_with_kill(cmd, timeout=cfg.timeout_retry_pdf)
                    md_path = tmp_out / base_name / f"{base_name}.md"
                    if md_path.is_file():
                        produced_md = md_path
                except Exception as e:
                    log.warning(f"    marker_single 失败: {e}")
            elif engine == "mineru":
                produced_md = _run_mineru_single(pdf_path, cfg, log)
            elif engine == "cloud":
                if not cfg.cloud_ocr.enabled:
                    log.info("    云端 OCR 未在配置中启用，跳过")
                    continue
                try:
                    ok = write_cloud_ocr_md(cfg, pdf_path, dest_md, dry_run=False, logger=log)
                except Exception as e:
                    log.warning(f"    云端 OCR 失败: {e}")
                    ok = False
                produced_md = dest_md if ok else None
            if produced_md:
                if produced_md != dest_md:
                    dest_md.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(produced_md), str(dest_md))
                _record_ocr_origin(cfg, dest_md)
                log.info(f"    成功 ({engine}): {dest_md.name}")
                failed_files[:] = [f for f in failed_files if not f.endswith(pdf_path.name)]
                if pdf_path.parent == lost_dir or lost_dir in pdf_path.parents:
                    pdf_path.unlink(missing_ok=True)
                    try:
                        pdf_path.parent.rmdir()
                    except Exception:
                        pass
                else:
                    _dispose_original(pdf_path, cfg, dry_run=False, log=log)
                success += 1
                break
        else:
            log.warning(f"    全部引擎失败: {pdf_path.name}")
            failed_files.append(str(dir_rel / pdf_path.name))
        for d in cleanup_dirs:
            if d.is_dir():
                shutil.rmtree(str(d), ignore_errors=True)
    log.info(f"[阶段3] 重试完成: {success}/{len(items)} 成功")
    return success


def process_pdfs(pdf_files: list[Path], scan_dir: Path, cfg: Config,
                 dry_run: bool, failed_files: list[str],
                 log: logging.Logger) -> int:
    if not pdf_files:
        log.info("没有需要 Marker 处理的 PDF 文件")
        return 0
    groups: dict[Path, list[Path]] = defaultdict(list)
    for pdf_path in pdf_files:
        groups[pdf_path.parent].append(pdf_path)
    total = len(pdf_files)
    log.info("=" * 50)
    log.info(f"[阶段2] Marker: 处理 {total} 个 PDF, 分布在 {len(groups)} 个目录")
    if dry_run:
        for dir_idx, (pdf_dir, pdfs) in enumerate(sorted(groups.items()), 1):
            log.info(f"  [{dir_idx}/{len(groups)}] 目录: {pdf_dir.relative_to(scan_dir)} ({len(pdfs)} PDF)")
            for p in pdfs:
                log.info(f"    -> [模拟] marker: {p.name}")
        return total

    lost_dir = cfg.ocr_work_dir / "lost"
    lost_dir.mkdir(parents=True, exist_ok=True)
    success_count = 0
    for dir_idx, (pdf_dir, pdfs) in enumerate(sorted(groups.items()), 1):
        dir_rel = pdf_dir.relative_to(scan_dir)
        num_batches = math.ceil(len(pdfs) / cfg.max_per_batch)
        log.info(f"  [{dir_idx}/{len(groups)}] 目录: {dir_rel} ({len(pdfs)} PDF, {num_batches} 批)")
        batch_input = cfg.ocr_work_dir / f"dir_{dir_idx}_in"
        batch_output = cfg.ocr_work_dir / f"dir_{dir_idx}_out"
        batch_input.mkdir(parents=True, exist_ok=True)
        batch_output.mkdir(parents=True, exist_ok=True)
        dir_success = 0
        processed = 0
        for batch_idx in range(num_batches):
            existing_pdfs = [f for f in batch_input.iterdir() if f.suffix.lower() == ".pdf"]
            if existing_pdfs:
                log.info(f"    [batch {batch_idx+1}/{num_batches}] 发现残留 {len(existing_pdfs)} PDF, 继续处理")
                batch_pdf_names = [f.stem for f in existing_pdfs]
            else:
                start = processed
                end = start + cfg.max_per_batch
                batch_pdfs = pdfs[start:end]
                batch_pdf_names = []
                for pdf_path in batch_pdfs:
                    try:
                        shutil.move(str(pdf_path), str(batch_input / pdf_path.name))
                        batch_pdf_names.append(pdf_path.stem)
                    except Exception as e:
                        rel = pdf_path.relative_to(scan_dir)
                        log.warning(f"    剪切失败: {rel}, 原因: {e}")
                        failed_files.append(str(rel))
                if not batch_pdf_names:
                    log.warning(f"    [batch {batch_idx+1}/{num_batches}] 无有效PDF, 跳过")
                    processed = end
                    continue
                log.info(f"    [batch {batch_idx+1}/{num_batches}] 剪切 {len(batch_pdf_names)} PDF, 开始转换...")
            cmd = build_marker_cmd(batch_input, batch_output, cfg)
            log.debug(f"    [batch {batch_idx+1}/{num_batches}] 命令: {' '.join(cmd)}")
            batch_start = time.time()
            try:
                returncode = run_with_kill(cmd, timeout=cfg.timeout_per_pdf * len(batch_pdf_names))
                batch_elapsed = time.time() - batch_start
                if returncode != 0:
                    log.warning(f"    [batch {batch_idx+1}/{num_batches}] marker失败 (退出码 {returncode}, {batch_elapsed:.1f}s)")
                else:
                    log.info(f"    [batch {batch_idx+1}/{num_batches}] marker完成 ({batch_elapsed:.1f}s)")
            except subprocess.TimeoutExpired:
                log.warning(f"    [batch {batch_idx+1}/{num_batches}] 超时, 已终止进程树")
            except Exception as e:
                log.error(f"    [batch {batch_idx+1}/{num_batches}] 异常: {e}")
            moved = collect_batch_results(batch_input, batch_output, pdf_dir, dir_rel,
                                          cfg, failed_files, lost_dir, log)
            dir_success += moved
            processed += len(batch_pdf_names)
            log.info(f"    [batch {batch_idx+1}/{num_batches}] 结果: {moved}/{len(batch_pdf_names)} 成功")
        log.info(f"  [{dir_idx}/{len(groups)}] 目录完成: {dir_success}/{len(pdfs)}")
        success_count += dir_success
    log.info(f"[阶段2] Marker 完成: {success_count}/{total} 成功")
    return success_count


def run_convert(cfg: Config, dry_run: bool = False,
                logger: logging.Logger | None = None,
                scan_dir: Path | None = None,
                engine: str | None = None) -> dict:
    """综合文档转 Markdown：先重试 lost，再 MarkItDown + Marker + 新失败重试。"""
    log = logger or logging.getLogger("kbimporter")
    if engine:
        cfg.engines = [engine]
        if engine == "cloud":
            cfg.cloud_ocr.enabled = True
            log.warning("已通过命令行显式启用云端 OCR（会产生 API 费用）")
    scan_dir = scan_dir or cfg.scan_dir
    if not scan_dir or not scan_dir.is_dir():
        log.error(f"扫描目录不存在: {scan_dir}")
        return {"error": "scan_dir_missing"}
    if not dry_run:
        ensure_dir(cfg.ocr_work_dir)

    failed_files: list[str] = []
    lost_dir = cfg.ocr_work_dir / "lost"
    lost_before: set[Path] = set()
    if lost_dir.is_dir():
        lost_before = {f for f in lost_dir.rglob("*.pdf") if f.parent != lost_dir}
    lost_retry_success = 0
    if lost_before:
        log.info("=" * 60)
        log.info(f"[阶段0] 先处理 lost 目录中的 {len(lost_before)} 个失败PDF")
        lost_retry_success = process_retry_engines(
            lost_dir, scan_dir, cfg, dry_run, failed_files, log
        )
    lost_still_failed = {f for f in lost_before if f.exists()}

    pdf_files, markitdown_files = find_files(scan_dir, cfg)
    total_all = len(pdf_files) + len(markitdown_files)
    log.info("=" * 60)
    log.info(f"综合文档转MD工具 ({'dry-run' if dry_run else '实际执行'})")
    log.info(f"扫描目录: {scan_dir}")
    log.info(f"扫描结果: {len(pdf_files)} 个 PDF, {len(markitdown_files)} 个其他文件 (共 {total_all} 个)")
    if total_all == 0 and not failed_files and lost_retry_success == 0:
        log.info("未找到任何可转换的文件")
        return {"total": 0, "markitdown": 0, "pdf": 0, "retry": 0, "failed": 0}

    md_success = process_markitdown(markitdown_files, scan_dir, cfg, dry_run, failed_files, log)
    marker_enabled = "marker" in cfg.engines
    if marker_enabled:
        pdf_success = process_pdfs(pdf_files, scan_dir, cfg, dry_run, failed_files, log)
        retry_success = process_retry_engines(
            lost_dir, scan_dir, cfg, dry_run, failed_files, log,
            skip_files=lost_still_failed,
        )
    else:
        pdf_success = 0
        retry_success = process_retry_engines(
            lost_dir, scan_dir, cfg, dry_run, failed_files, log,
            pending_pdfs=pdf_files, skip_files=lost_still_failed,
        )
    pdf_success += lost_retry_success + retry_success

    if not dry_run:
        for item in cfg.ocr_work_dir.iterdir():
            if item.name == "lost":
                continue
            try:
                if item.is_dir():
                    _force_rmtree(item)
                elif item.is_file():
                    try:
                        item.unlink()
                    except OSError:
                        os.chmod(item, stat.S_IWRITE)
                        item.unlink()
            except Exception as e:
                log.warning(f"清理失败: {item.name}, 原因: {e}")
        remaining_lost = [f for f in lost_dir.rglob("*.pdf") if f.parent != lost_dir] if lost_dir.is_dir() else []
        if remaining_lost:
            log.info(f"仍有 {len(remaining_lost)} 个失败PDF保留在: {lost_dir}")
    unique_failed = list(dict.fromkeys(failed_files))
    log.info("=" * 60)
    log.info(f"完成: MarkItDown {md_success}/{len(markitdown_files)}, "
             f"PDF {pdf_success}/{len(pdf_files)} (lost重试 "
             f"{lost_retry_success + retry_success}), 失败 {len(unique_failed)}")
    for f in unique_failed:
        log.info(f"  - {f}")
    return {
        "total": total_all,
        "markitdown": md_success,
        "pdf": pdf_success,
        "retry": lost_retry_success + retry_success,
        "failed": len(unique_failed),
    }
