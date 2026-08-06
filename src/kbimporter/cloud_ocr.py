from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from kbimporter.config import Config
from kbimporter.util import ensure_dir


def _fitz():
    try:
        import fitz
    except ImportError:
        raise RuntimeError("缺少依赖 PyMuPDF：pip install kbimporter[sync] 或 pip install PyMuPDF")
    return fitz


def pdf_page_count(pdf_path: str | Path) -> int:
    fitz = _fitz()
    doc = fitz.open(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def render_page(pdf_path: str | Path, page_index: int, scale: float) -> bytes:
    fitz = _fitz()
    doc = fitz.open(str(pdf_path))
    try:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        return pix.tobytes("png")
    finally:
        doc.close()


def _b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


# ---------------------------------------------------------------- OpenAI 兼容

def _openai_batch(cfg: Config, api_key: str, images: list[bytes],
                  log: logging.Logger) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("缺少依赖 openai：pip install kbimporter[cloud] 或 pip install openai")
    oai = cfg.cloud_ocr.openai
    client = OpenAI(api_key=api_key, base_url=oai.base_url, timeout=oai.timeout)
    content: list[dict] = []
    for img in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_b64(img)}"},
        })
    content.append({"type": "text", "text": oai.prompt})
    last_err: Exception | None = None
    for attempt in range(1, oai.max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=oai.model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            log.warning(f"OpenAI 兼容 OCR 第 {attempt} 次失败: {e}")
            if attempt < oai.max_retries:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"OpenAI 兼容 OCR 失败: {last_err}")


# --------------------------------------------------------------------- 百度

_baidu_token_cache: dict[str, tuple[str, float]] = {}


def _baidu_token(cfg: Config, log: logging.Logger) -> str:
    bd = cfg.cloud_ocr.baidu
    cache_key = f"{bd.api_key_env}:{bd.secret_key_env}"
    now = time.time()
    cached = _baidu_token_cache.get(cache_key)
    if cached and cached[1] > now + 60:
        return cached[0]
    api_key = os.environ.get(bd.api_key_env, "")
    secret = os.environ.get(bd.secret_key_env, "")
    if not api_key or not secret:
        raise RuntimeError(
            f"百度 OCR 缺少密钥：请设置环境变量 {bd.api_key_env} 和 {bd.secret_key_env}"
        )
    query = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": secret,
    })
    url = f"{bd.token_url}?{query}"
    last_err: Exception | None = None
    for attempt in range(1, bd.max_retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=bd.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"百度 token 响应异常: {data}")
            expires_in = float(data.get("expires_in", 2592000))
            _baidu_token_cache[cache_key] = (token, now + expires_in)
            return token
        except Exception as e:
            last_err = e
            log.warning(f"百度 token 获取第 {attempt} 次失败: {e}")
            if attempt < bd.max_retries:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"百度 OCR token 获取失败: {last_err}")


def _baidu_page(cfg: Config, token: str, image_bytes: bytes,
                log: logging.Logger) -> str:
    bd = cfg.cloud_ocr.baidu
    params = {
        "image": _b64(image_bytes),
        "language_type": bd.language_type,
        "detect_direction": "true" if bd.detect_direction else "false",
    }
    if bd.paragraph:
        params["paragraph"] = "true"
    url = f"{bd.accurate_url}?access_token={urllib.parse.quote(token)}"
    body = urllib.parse.urlencode(params).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(1, bd.max_retries + 1):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=bd.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "error_code" in data:
                raise RuntimeError(f"百度 OCR 错误: {data.get('error_code')} {data.get('error_msg')}")
            words = [w.get("words", "") for w in data.get("words_result", [])]
            paragraphs = data.get("paragraphs_result")
            if paragraphs:
                lines = []
                for p in paragraphs:
                    idxs = p.get("words_result_idx", [])
                    lines.append("".join(words[i] for i in idxs if i < len(words)))
                return "\n\n".join(lines)
            return "\n".join(words)
        except Exception as e:
            last_err = e
            log.warning(f"百度 OCR 第 {attempt} 次失败: {e}")
            if attempt < bd.max_retries:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"百度 OCR 失败: {last_err}")


# -------------------------------------------------------------- PaddleOCR 云 API

def _requests():
    try:
        import requests
    except ImportError:
        raise RuntimeError("缺少依赖 requests：pip install requests")
    return requests


def _paddle_headers(cfg: Config) -> dict:
    token = os.environ.get(cfg.cloud_ocr.paddle.api_key_env, "")
    if not token:
        raise RuntimeError(
            f"缺少 PaddleOCR 云 API 密钥：请设置环境变量 "
            f"{cfg.cloud_ocr.paddle.api_key_env}"
        )
    return {"Authorization": f"bearer {token}"}


def _paddle_submit(cfg: Config, pdf_path: str | Path,
                   log: logging.Logger) -> str:
    requests = _requests()
    pdl = cfg.cloud_ocr.paddle
    headers = _paddle_headers(cfg)
    optional_payload = {
        "useDocOrientationClassify": pdl.use_doc_orientation_classify,
        "useDocUnwarping": pdl.use_doc_unwarping,
        "useChartRecognition": pdl.use_chart_recognition,
    }
    data = {
        "model": pdl.model,
        "optionalPayload": json.dumps(optional_payload),
    }
    last_err: Exception | None = None
    for attempt in range(1, pdl.max_retries + 1):
        try:
            with open(pdf_path, "rb") as f:
                files = {"file": (Path(pdf_path).name, f)}
                resp = requests.post(
                    pdl.job_url, headers=headers, data=data, files=files,
                    timeout=pdl.timeout,
                )
            if resp.status_code == 200:
                job_id = resp.json()["data"]["jobId"]
                log.info(f"PaddleOCR 云任务已提交: jobId={job_id}")
                return job_id
            last_err = RuntimeError(
                f"PaddleOCR 提交失败: {resp.status_code} {resp.text[:300]}"
            )
            log.warning(f"  PaddleOCR 提交第 {attempt} 次失败: {resp.status_code}")
        except Exception as e:
            last_err = e
            log.warning(f"  PaddleOCR 提交第 {attempt} 次异常: {e}")
        if attempt < pdl.max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"PaddleOCR 提交失败（已重试 {pdl.max_retries} 次）: {last_err}"
    )


def _paddle_poll(cfg: Config, job_id: str, log: logging.Logger) -> dict:
    requests = _requests()
    pdl = cfg.cloud_ocr.paddle
    headers = _paddle_headers(cfg)
    deadline = time.time() + pdl.max_poll_seconds
    while time.time() < deadline:
        resp = requests.get(
            f"{pdl.job_url}/{job_id}", headers=headers, timeout=pdl.timeout
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        state = data.get("state")
        if state == "done":
            return data
        if state == "failed":
            raise RuntimeError(f"PaddleOCR 云任务失败: {data.get('errorMsg')}")
        prog = data.get("extractProgress", {})
        log.info(
            f"  PaddleOCR 任务运行中: {prog.get('extractedPages')}/"
            f"{prog.get('totalPages')} 页"
        )
        time.sleep(pdl.poll_interval)
    raise RuntimeError("PaddleOCR 云任务超时")


def _paddle_download_pages(cfg: Config, data: dict,
                           log: logging.Logger) -> list[str]:
    requests = _requests()
    jsonl_url = data["resultUrl"]["jsonUrl"]
    resp = requests.get(jsonl_url, timeout=cfg.cloud_ocr.paddle.timeout)
    resp.raise_for_status()
    pages: list[str] = []
    for line in resp.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        result = json.loads(line).get("result", {})
        texts = [
            r.get("markdown", {}).get("text", "")
            for r in result.get("layoutParsingResults", [])
        ]
        pages.append("\n\n".join(t for t in texts if t))
    log.info(f"PaddleOCR 结果下载完成: {len(pages)} 页")
    return pages


def _paddle_ocr_job(cfg: Config, pdf_path: str | Path,
                    log: logging.Logger) -> str:
    """PaddleOCR 云端异步任务：提交整份 PDF -> 轮询 -> 下载 JSONL -> 合并。"""
    state_dir = _state_dir(cfg, pdf_path)
    cp = _load_checkpoint(state_dir) or {}
    if cp.get("kind") == "paddle" and cp.get("total_pages") and \
            len(cp.get("done_pages", [])) >= cp["total_pages"]:
        log.info("PaddleOCR 任务已完成，直接合并缓存")
        return _merge_parts(state_dir, cp["total_pages"], 1)

    pdl = cfg.cloud_ocr.paddle
    last_err: Exception | None = None
    job_id = cp.get("job_id")
    for attempt in range(1, pdl.max_retries + 1):
        if job_id is None or attempt > 1:
            job_id = _paddle_submit(cfg, pdf_path, log)
            _save_checkpoint_paddle(state_dir, {"job_id": job_id, "done_pages": []})
        try:
            data = _paddle_poll(cfg, job_id, log)
            break
        except RuntimeError as e:
            last_err = e
            log.warning(f"  PaddleOCR 任务第 {attempt} 次失败: {e}")
            job_id = None
    else:
        raise RuntimeError(
            f"PaddleOCR 云任务失败（已重试 {pdl.max_retries} 次）: {last_err}。"
            "可能原因：PDF 加密/损坏、PaddleOCR 服务繁忙、API Key 失效。"
            "可稍后重试，或换用其他 OCR 引擎（kb ocr mode local）"
        )
    pages = _paddle_download_pages(cfg, data, log)
    total = len(pages)
    done_pages = set(cp.get("done_pages", []))
    for idx, text in enumerate(pages):
        if idx in done_pages:
            continue
        _save_part(state_dir, f"{idx:04d}-{idx + 1:04d}", text)
        done_pages.add(idx)
        _save_checkpoint_paddle(state_dir, {
            "job_id": job_id, "total_pages": total,
            "done_pages": sorted(done_pages),
        })
    return _merge_parts(state_dir, total, 1)


def _save_checkpoint_paddle(state_dir: Path, payload: dict):
    payload["kind"] = "paddle"
    tmp = state_dir / "checkpoint.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_dir / "checkpoint.json")


# ---------------------------------------------------------------- 断点续传

def _unit_parts(total: int, batch_size: int) -> list[tuple[int, int]]:
    return [(s, min(s + batch_size, total)) for s in range(0, total, batch_size)]


def _file_id(pdf_path: str | Path) -> str:
    return hashlib.sha256(str(pdf_path).encode("utf-8")).hexdigest()[:16]


def _state_dir(cfg: Config, pdf_path: str | Path) -> Path:
    base = cfg.cloud_ocr.state_dir or Path(".kb") / "cloud_ocr"
    return ensure_dir(base / _file_id(pdf_path))


def _load_checkpoint(state_dir: Path) -> dict | None:
    cp = state_dir / "checkpoint.json"
    if not cp.exists():
        return None
    try:
        return json.loads(cp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_checkpoint(state_dir: Path, total: int, batch_size: int, done: list[str]):
    tmp = state_dir / "checkpoint.json.tmp"
    tmp.write_text(
        json.dumps({"total": total, "batch_size": batch_size, "done": done},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    tmp.replace(state_dir / "checkpoint.json")


def _part_path(state_dir: Path, unit_id: str) -> Path:
    return state_dir / "parts" / f"{unit_id}.txt"


def _save_part(state_dir: Path, unit_id: str, text: str):
    p = _part_path(state_dir, unit_id)
    ensure_dir(p.parent)
    p.write_text(text, encoding="utf-8")


def _merge_parts(state_dir: Path, total: int, batch_size: int) -> str:
    parts = []
    for s, e in _unit_parts(total, batch_size):
        p = _part_path(state_dir, f"{s:04d}-{e:04d}")
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").strip())
    return "\n\n".join(x for x in parts if x)


# --------------------------------------------------------------------- 主流程

def ocr_pdf_cloud(cfg: Config, pdf_path: str | Path, dry_run: bool = False,
                  logger: logging.Logger | None = None) -> str | None:
    """云端 OCR 一个 PDF，返回合并后的 Markdown 文本。

    - 必须通过配置 [cloud_ocr].enabled=true 显式开启（会产生 API 费用）。
    - 按批处理，断点续传；中断后再次运行不会重复已完成的页。
    - dry_run 只报告页数与预计请求数，不调用 API。
    """
    log = logger or logging.getLogger("kbimporter")
    if not cfg.cloud_ocr.enabled:
        raise RuntimeError(
            "云端 OCR 未启用：请在配置 [cloud_ocr] enabled=true 显式开启"
            "（风险：付费 API 费用 + 文档图片发送到第三方服务）"
        )
    provider = cfg.cloud_ocr.provider
    if dry_run:
        if provider == "paddle":
            try:
                n = pdf_page_count(pdf_path)
                log.info(f"[dry-run] 将提交整份 PDF（约 {n} 页）到 PaddleOCR 云端异步任务（不调用 API）")
            except Exception:
                log.info("[dry-run] 将提交整份 PDF 到 PaddleOCR 云端异步任务（不调用 API）")
        else:
            total = pdf_page_count(pdf_path)
            batch_size = cfg.cloud_ocr.openai.page_batch_size if provider == "openai" else 1
            units = _unit_parts(total, batch_size)
            log.info(f"云端 OCR: {Path(pdf_path).name} 共 {total} 页, {len(units)} 次请求, provider={provider}")
            log.info(f"[dry-run] 预计 {len(units)} 次 API 请求（不会调用）")
        return None
    log.warning("云端 OCR 将调用付费 API，并把文档图片发送到第三方服务（配置已显式开启）")

    if provider == "paddle":
        return _paddle_ocr_job(cfg, pdf_path, log)

    total = pdf_page_count(pdf_path)
    batch_size = cfg.cloud_ocr.openai.page_batch_size if provider == "openai" else 1
    units = _unit_parts(total, batch_size)
    log.info(f"云端 OCR: {Path(pdf_path).name} 共 {total} 页, {len(units)} 次请求, provider={provider}")
    state_dir = _state_dir(cfg, pdf_path)
    cp = _load_checkpoint(state_dir)
    if cp and cp.get("total") != total:
        log.info("页数已变化，重置断点")
        cp = None
    done = set(cp.get("done", [])) if cp else set()
    pending = [u for u in units if f"{u[0]:04d}-{u[1]:04d}" not in done]
    if not pending:
        log.info("所有批次已完成，直接合并")
        return _merge_parts(state_dir, total, batch_size)

    if provider == "openai":
        api_key = os.environ.get(cfg.cloud_ocr.openai.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"缺少 OpenAI 兼容 OCR 密钥：请设置环境变量 "
                f"{cfg.cloud_ocr.openai.api_key_env}"
            )
        for s, e in pending:
            unit_id = f"{s:04d}-{e:04d}"
            images = [render_page(pdf_path, i, cfg.cloud_ocr.openai.scale_factor) for i in range(s, e)]
            text = _openai_batch(cfg, api_key, images, log)
            _save_part(state_dir, unit_id, text)
            done.add(unit_id)
            _save_checkpoint(state_dir, total, batch_size, sorted(done))
            log.info(f"  完成 {unit_id} ({len(done)}/{len(units)})")
    elif provider == "baidu":
        token = _baidu_token(cfg, log)
        bd = cfg.cloud_ocr.baidu
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=bd.max_workers) as pool:
            future_map = {
                pool.submit(
                    _baidu_page, cfg, token,
                    render_page(pdf_path, s, bd.scale_factor), log
                ): (s, e)
                for s, e in pending
            }
            for fut in as_completed(future_map):
                s, e = future_map[fut]
                unit_id = f"{s:04d}-{e:04d}"
                try:
                    text = fut.result()
                    _save_part(state_dir, unit_id, text)
                    done.add(unit_id)
                    _save_checkpoint(state_dir, total, batch_size, sorted(done))
                    log.info(f"  完成 {unit_id} ({len(done)}/{len(units)})")
                except Exception as exc:
                    failures.append(unit_id)
                    log.warning(f"  失败 {unit_id}: {exc}")
        if failures:
            raise RuntimeError(f"百度 OCR 有 {len(failures)} 个批次失败: {failures[:10]}")
    else:
        raise RuntimeError(f"未知云端 OCR provider: {provider}（可选 openai / baidu）")

    return _merge_parts(state_dir, total, batch_size)


def write_cloud_ocr_md(cfg: Config, pdf_path: str | Path, dest_md: str | Path,
                       dry_run: bool = False,
                       logger: logging.Logger | None = None) -> bool:
    """云端 OCR 并把结果写入目标 .md。返回是否成功。"""
    log = logger or logging.getLogger("kbimporter")
    text = ocr_pdf_cloud(cfg, pdf_path, dry_run=dry_run, logger=log)
    if dry_run:
        return False
    if not text or not text.strip():
        log.warning(f"云端 OCR 结果为空: {Path(pdf_path).name}")
        return False
    dest = Path(dest_md)
    ensure_dir(dest.parent)
    dest.write_text(text, encoding="utf-8")
    log.info(f"云端 OCR 完成 -> {dest}")
    return True
