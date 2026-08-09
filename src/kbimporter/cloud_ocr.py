from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

from kbimporter.config import Config
from kbimporter.util import ensure_dir


class CloudQuotaError(RuntimeError):
    """云端 OCR 当日额度耗尽（如 PaddleOCR 429 / MinerU -60018）。

    重试无意义，应跳过当前 provider 的内部重试，立即切换到下一个通道。
    """


class JobCancelledError(RuntimeError):
    """并发子任务因其他子任务失败或额度耗尽而被取消。"""


def _looks_like_quota_error(status_code: int | None = None, text: str = "",
                            quota_codes: tuple[str, ...] = ()) -> bool:
    """只有 HTTP 429 或明确的额度错误码（如 MinerU -60018）才算额度耗尽。

    普通错误（500/503/504、文件损坏、解析失败等）一律返回 False，走正常重试。
    """
    if status_code == 429:
        return True
    lower = (text or "").lower()
    return any(code.lower() in lower for code in quota_codes)


_QUOTA_EXHAUSTED: set[str] = set()


def _mark_quota_exhausted(provider: str):
    """记录某 provider 今日额度已用完，本次运行剩余文件直接跳过。"""
    _QUOTA_EXHAUSTED.add(provider)


def _quota_exhausted(provider: str) -> bool:
    return provider in _QUOTA_EXHAUSTED


def _clear_quota_flags():
    _QUOTA_EXHAUSTED.clear()


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
                   log: logging.Logger,
                   task_tag: str | None = None) -> str:
    requests = _requests()
    pdl = cfg.cloud_ocr.paddle
    headers = _paddle_headers(cfg)
    tag = f" [子任务{task_tag}]" if task_tag else ""
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
            if _looks_like_quota_error(resp.status_code, resp.text):
                raise CloudQuotaError(
                    f"PaddleOCR 当日解析额度已用完（HTTP {resp.status_code}）: "
                    f"{resp.text[:300]}"
                )
            if resp.status_code == 200:
                job_id = resp.json()["data"]["jobId"]
                log.info(f"PaddleOCR 云任务已提交{tag}: jobId={job_id}")
                return job_id
            last_err = RuntimeError(
                f"PaddleOCR 提交失败: {resp.status_code} {resp.text[:300]}"
            )
            log.warning(f"  PaddleOCR{tag} 提交第 {attempt} 次失败: {resp.status_code}")
        except CloudQuotaError:
            raise
        except Exception as e:
            last_err = e
            log.warning(f"  PaddleOCR{tag} 提交第 {attempt} 次异常: {e}")
        if attempt < pdl.max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"PaddleOCR 提交失败（已重试 {pdl.max_retries} 次）: {last_err}"
    )


def _paddle_poll(cfg: Config, job_id: str, log: logging.Logger,
                 cancel_event: threading.Event | None = None,
                 task_tag: str | None = None) -> dict:
    requests = _requests()
    pdl = cfg.cloud_ocr.paddle
    headers = _paddle_headers(cfg)
    tag = f" [子任务{task_tag}]" if task_tag else ""
    deadline = time.time() + pdl.max_poll_seconds
    last_progress = -1
    last_progress_time = time.time()
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("PaddleOCR 轮询已取消（并发子任务中止）")
        resp = requests.get(
            f"{pdl.job_url}/{job_id}", headers=headers, timeout=pdl.timeout
        )
        if _looks_like_quota_error(resp.status_code, resp.text):
            raise CloudQuotaError(
                f"PaddleOCR 当日解析额度已用完（HTTP {resp.status_code}）: "
                f"{resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"PaddleOCR{tag} 轮询请求失败: HTTP {resp.status_code} "
                f"{resp.text[:300]}"
            )
        try:
            data = resp.json().get("data", {})
        except Exception as e:
            raise RuntimeError(f"PaddleOCR{tag} 轮询响应解析失败: {e}") from e
        state = data.get("state")
        if state == "done":
            return data
        if state == "failed":
            raise RuntimeError(f"PaddleOCR 云任务失败: {data.get('errorMsg')}")
        prog = data.get("extractProgress", {})
        extracted = prog.get("extractedPages") or 0
        now = time.time()
        if extracted != last_progress:
            last_progress = extracted
            last_progress_time = now
        elif now - last_progress_time >= pdl.stall_timeout:
            raise RuntimeError(
                f"PaddleOCR{tag} 任务进度停滞超过 {pdl.stall_timeout}s"
                f"（extractedPages={extracted}），重新提交"
            )
        log.info(
            f"  PaddleOCR{tag} 任务运行中: {extracted}/"
            f"{prog.get('totalPages')} 页 (jobId={job_id})"
        )
        time.sleep(pdl.poll_interval)
    raise RuntimeError("PaddleOCR 云任务超时")


def _paddle_download_pages(cfg: Config, data: dict,
                           log: logging.Logger,
                           task_tag: str | None = None) -> list[str]:
    requests = _requests()
    tag = f" [子任务{task_tag}]" if task_tag else ""
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
    log.info(f"PaddleOCR{tag} 结果下载完成: {len(pages)} 页")
    return pages


def _paddle_ocr_job(cfg: Config, pdf_path: str | Path,
                    log: logging.Logger,
                    cancel_event: threading.Event | None = None,
                    task_tag: str | None = None) -> str:
    """PaddleOCR 云端异步任务：提交整份 PDF -> 轮询 -> 下载 JSONL -> 合并。"""
    tag = f" [子任务{task_tag}]" if task_tag else ""
    state_dir = _state_dir(cfg, pdf_path)
    _prepare_provider_state(state_dir, "paddle")
    cp = _load_checkpoint(state_dir) or {}
    current_total = pdf_page_count(pdf_path)
    if cp.get("kind") == "paddle" and cp.get("total_pages") and \
            len(cp.get("done_pages", [])) >= cp["total_pages"]:
        if cp["total_pages"] == current_total:
            log.info("PaddleOCR 任务已完成，直接合并缓存")
            return _merge_parts(state_dir, cp["total_pages"], 1)
        log.info("页数已变化，重置断点")
        _clear_paddle_state(state_dir)
        cp = {}

    pdl = cfg.cloud_ocr.paddle
    last_err: Exception | None = None
    job_id = cp.get("job_id")
    for attempt in range(1, pdl.max_retries + 1):
        if job_id is None or attempt > 1:
            job_id = _paddle_submit(cfg, pdf_path, log, task_tag=task_tag)
            _save_checkpoint_paddle(state_dir, {"job_id": job_id, "done_pages": []})
        try:
            data = _paddle_poll(
                cfg, job_id, log, cancel_event, task_tag=task_tag
            )
            break
        except JobCancelledError:
            raise
        except CloudQuotaError:
            raise
        except RuntimeError as e:
            last_err = e
            log.warning(f"  PaddleOCR{tag} 任务第 {attempt} 次失败: {e}")
            job_id = None
    else:
        raise RuntimeError(
            f"PaddleOCR 云任务失败（已重试 {pdl.max_retries} 次）: {last_err}。"
            "可能原因：PDF 加密/损坏、PaddleOCR 服务繁忙、API Key 失效。"
            "可稍后重试，或换用其他 OCR 引擎（kb ocr mode local）"
        )
    pages = _paddle_download_pages(cfg, data, log, task_tag=task_tag)
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


def _clear_paddle_state(state_dir: Path):
    """清除 PaddleOCR 任务的断点与页缓存（用于页数变化后重新处理）。"""
    _clear_state(state_dir)


def _clear_state(state_dir: Path):
    """清除某个 provider 的断点与页缓存（用于页数变化或 provider 切换后重新处理）。"""
    cp = state_dir / "checkpoint.json"
    try:
        cp.unlink(missing_ok=True)
    except OSError:
        pass
    parts = state_dir / "parts"
    if parts.exists():
        shutil.rmtree(parts, ignore_errors=True)


def _provider_kind(provider: str) -> str:
    """按页/批请求的 provider（openai/baidu）共用一套断点格式，异步整档任务各自独立。"""
    if provider in ("openai", "baidu"):
        return "page"
    return provider


def _prepare_provider_state(state_dir: Path, provider: str):
    """切换 provider 时清掉旧断点与页缓存，避免不同 API 的结果混用。"""
    kind = _provider_kind(provider)
    cp = _load_checkpoint(state_dir)
    cp_kind = (cp or {}).get("kind")
    if cp_kind is None:
        if kind == "page":
            return  # 旧版 page 断点可继续复用
        _clear_state(state_dir)
        return
    if cp_kind != kind:
        _clear_state(state_dir)


def _split_pdf(pdf_path: str | Path, out_dir: Path, max_pages: int) -> list[Path]:
    """按页把 PDF 拆成多个不超过 max_pages 页的子文件，返回按页序排列的路径。"""
    fitz = _fitz()
    src = fitz.open(str(pdf_path))
    try:
        total = len(src)
        parts: list[Path] = []
        for start in range(0, total, max_pages):
            end = min(start + max_pages, total)
            out = out_dir / f"{Path(pdf_path).stem}_p{start + 1:04d}-{end:04d}.pdf"
            new = fitz.open()
            try:
                new.insert_pdf(src, from_page=start, to_page=end - 1)
                new.save(str(out), garbage=3, deflate=True)
            finally:
                new.close()
            parts.append(out)
        return parts
    finally:
        src.close()


def _run_split_sub_jobs(cfg: Config, parts: list[Path], job_func, log: logging.Logger,
                        max_workers: int, label: str) -> list[str]:
    """按波次并发运行子任务，保持输入顺序；任一失败即取消剩余波次。

    job_func(cfg, sub, log, cancel_event, task_tag) -> str
    额度耗尽（CloudQuotaError）优先级最高：立即停止后续波次并原样上抛。
    """
    texts: list[str | None] = [None] * len(parts)
    wave_size = max(1, max_workers)
    first_error: BaseException | None = None
    for start in range(0, len(parts), wave_size):
        wave = list(enumerate(parts[start:start + wave_size], start))
        for idx, sub in wave:
            sub_total = pdf_page_count(sub)
            log.info(
                f"  {label} 子任务 [{idx + 1}/{len(parts)}]: "
                f"{sub.name}（{sub_total} 页）"
            )
        cancel_event = threading.Event()
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {
                pool.submit(
                    job_func, cfg, sub, log, cancel_event,
                    f"{idx + 1}/{len(parts)}",
                ): idx
                for idx, sub in wave
            }
            pending = set(futures)
            while pending and not cancel_event.is_set():
                done, pending = wait(
                    pending, timeout=5, return_when=FIRST_COMPLETED
                )
                for fut in done:
                    idx = futures[fut]
                    try:
                        texts[idx] = fut.result()
                    except CloudQuotaError as e:
                        first_error = e
                        cancel_event.set()
                        for f in pending:
                            f.cancel()
                        break
                    except Exception as e:
                        if not isinstance(first_error, CloudQuotaError):
                            first_error = e
                        cancel_event.set()
                        for f in pending:
                            f.cancel()
                        break
                if first_error is not None:
                    break
        if first_error is not None:
            raise first_error
    return [t for t in texts if t is not None]


def _paddle_ocr_split_job(cfg: Config, pdf_path: str | Path,
                          log: logging.Logger) -> str:
    """PaddleOCR 云端任务：超过单任务页数上限时自动拆分，识别后按页序合并。"""
    pdl = cfg.cloud_ocr.paddle
    total = pdf_page_count(pdf_path)
    main_state = _state_dir(cfg, pdf_path)
    cp = _load_checkpoint(main_state) or {}
    if cp.get("kind") == "paddle" and cp.get("total_pages") == total and \
            len(cp.get("done_pages", [])) >= total:
        log.info("PaddleOCR 任务已完成，直接合并缓存")
        return _merge_parts(main_state, total, 1)
    if total <= pdl.max_pages_per_task:
        return _paddle_ocr_job(cfg, pdf_path, log)

    log.warning(
        f"PaddleOCR 单任务建议不超过 {pdl.max_pages_per_task} 页；"
        f"当前 {total} 页，将拆分为多个子任务，识别完成后自动合并"
    )
    split_dir = main_state / "split"
    ensure_dir(split_dir)
    parts = _split_pdf(pdf_path, split_dir, pdl.max_pages_per_task)
    log.info(f"PaddleOCR 子任务并发数: {pdl.max_workers}")
    texts = _run_split_sub_jobs(
        cfg, parts, _paddle_ocr_job, log, pdl.max_workers, "PaddleOCR"
    )
    return "\n\n".join(t for t in texts if t and t.strip())


def _save_checkpoint_paddle(state_dir: Path, payload: dict):
    payload["kind"] = "paddle"
    tmp = state_dir / "checkpoint.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_dir / "checkpoint.json")


# -------------------------------------------------------------- MinerU 云 API

def _mineru_headers(cfg: Config) -> dict:
    token = os.environ.get(cfg.cloud_ocr.mineru.api_key_env, "")
    if not token:
        raise RuntimeError(
            f"缺少 MinerU 云 API 密钥：请设置环境变量 "
            f"{cfg.cloud_ocr.mineru.api_key_env}"
        )
    return {"Authorization": f"Bearer {token}"}


def _mineru_apply_upload(cfg: Config, pdf_path: str | Path,
                         log: logging.Logger,
                         task_tag: str | None = None) -> tuple[str, str]:
    """申请 MinerU 文件上传链接，返回 (batch_id, upload_url)。"""
    requests = _requests()
    mnr = cfg.cloud_ocr.mineru
    headers = _mineru_headers(cfg)
    headers["Content-Type"] = "application/json"
    tag = f" [子任务{task_tag}]" if task_tag else ""
    payload = {
        "files": [{
            "name": Path(pdf_path).name,
            "data_id": f"kb_{_file_id(pdf_path)}",
            "is_ocr": mnr.is_ocr,
        }],
        "model_version": mnr.model_version,
        "enable_formula": mnr.enable_formula,
        "enable_table": mnr.enable_table,
        "language": mnr.language,
    }
    last_err: Exception | None = None
    for attempt in range(1, mnr.max_retries + 1):
        try:
            resp = requests.post(
                mnr.upload_url, headers=headers, json=payload, timeout=mnr.timeout
            )
            if _looks_like_quota_error(
                resp.status_code, resp.text, quota_codes=("60018",)
            ):
                raise CloudQuotaError(
                    f"MinerU 当日解析额度已用完（HTTP {resp.status_code}）: "
                    f"{resp.text[:300]}"
                )
            data = resp.json() if resp.status_code == 200 else {}
            if resp.status_code == 200 and data.get("code") == 0:
                batch_id = data["data"]["batch_id"]
                urls = data["data"].get("file_urls") or []
                if not urls:
                    raise RuntimeError("MinerU 申请上传链接未返回 file_urls")
                log.info(f"MinerU 已申请上传链接{tag}: batch_id={batch_id}")
                return batch_id, urls[0]
            last_err = RuntimeError(
                f"MinerU 申请上传链接失败: {resp.status_code} {resp.text[:300]}"
            )
            log.warning(f"  MinerU{tag} 申请上传链接第 {attempt} 次失败: {resp.status_code}")
        except CloudQuotaError:
            raise
        except Exception as e:
            last_err = e
            log.warning(f"  MinerU{tag} 申请上传链接第 {attempt} 次异常: {e}")
        if attempt < mnr.max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"MinerU 申请上传链接失败（已重试 {mnr.max_retries} 次）: {last_err}"
    )


def _mineru_upload(cfg: Config, pdf_path: str | Path, upload_url: str,
                   log: logging.Logger):
    """把本地 PDF PUT 到 MinerU 返回的上传链接（无需 Content-Type）。"""
    requests = _requests()
    mnr = cfg.cloud_ocr.mineru
    last_err: Exception | None = None
    for attempt in range(1, mnr.max_retries + 1):
        try:
            with open(pdf_path, "rb") as f:
                resp = requests.put(upload_url, data=f, timeout=mnr.timeout)
            if resp.status_code == 200:
                log.info(f"MinerU 文件上传完成: {Path(pdf_path).name}")
                return
            last_err = RuntimeError(
                f"MinerU 文件上传失败: {resp.status_code} {resp.text[:300]}"
            )
            log.warning(f"  MinerU 文件上传第 {attempt} 次失败: {resp.status_code}")
        except Exception as e:
            last_err = e
            log.warning(f"  MinerU 文件上传第 {attempt} 次异常: {e}")
        if attempt < mnr.max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"MinerU 文件上传失败（已重试 {mnr.max_retries} 次）: {last_err}"
    )


def _mineru_poll(cfg: Config, batch_id: str, log: logging.Logger,
                 cancel_event: threading.Event | None = None,
                 task_tag: str | None = None) -> str:
    """轮询 MinerU 批量解析结果，返回 full_zip_url。"""
    requests = _requests()
    mnr = cfg.cloud_ocr.mineru
    headers = _mineru_headers(cfg)
    headers["Content-Type"] = "application/json"
    tag = f" [子任务{task_tag}]" if task_tag else ""
    deadline = time.time() + mnr.max_poll_seconds
    last_progress = -1
    last_progress_time = time.time()
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelledError("MinerU 轮询已取消（并发子任务中止）")
        resp = requests.get(
            f"{mnr.result_url}/{batch_id}", headers=headers, timeout=mnr.timeout
        )
        if _looks_like_quota_error(
            resp.status_code, resp.text, quota_codes=("60018",)
        ):
            raise CloudQuotaError(
                f"MinerU 当日解析额度已用完（HTTP {resp.status_code}）: "
                f"{resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"MinerU{tag} 轮询请求失败: HTTP {resp.status_code} "
                f"{resp.text[:300]}"
            )
        try:
            data = resp.json().get("data", {})
        except Exception as e:
            raise RuntimeError(f"MinerU{tag} 轮询响应解析失败: {e}") from e
        results = data.get("extract_result") or []
        if not results:
            if time.time() - last_progress_time >= mnr.stall_timeout:
                raise RuntimeError(
                    f"MinerU{tag} 任务进度停滞超过 {mnr.stall_timeout}s"
                    f"（无任务结果, batch_id={batch_id}）"
                )
            time.sleep(mnr.poll_interval)
            continue
        item = results[0]
        state = item.get("state")
        if state == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise RuntimeError("MinerU 任务完成但未返回 full_zip_url")
            log.info(f"MinerU 任务完成: {item.get('file_name', '')}")
            return zip_url
        if state == "failed":
            err_msg = item.get("err_msg") or item.get("errMsg") or ""
            if _looks_like_quota_error(None, err_msg, quota_codes=("60018",)):
                raise CloudQuotaError(f"MinerU 云任务失败（额度/上限）: {err_msg}")
            raise RuntimeError(
                f"MinerU 云任务失败: {err_msg or '未知原因'}"
            )
        prog = item.get("extract_progress") or {}
        extracted = prog.get("extracted_pages") or 0
        now = time.time()
        if extracted != last_progress:
            last_progress = extracted
            last_progress_time = now
        elif now - last_progress_time >= mnr.stall_timeout:
            raise RuntimeError(
                f"MinerU{tag} 任务进度停滞超过 {mnr.stall_timeout}s"
                f"（extracted_pages={extracted}），重新提交"
            )
        log.info(
            f"  MinerU{tag} 任务运行中: {extracted}/"
            f"{prog.get('total_pages')} 页 (batch_id={batch_id})"
        )
        time.sleep(mnr.poll_interval)
    raise RuntimeError("MinerU 云任务超时")


def _mineru_download_md(cfg: Config, zip_url: str,
                        log: logging.Logger,
                        task_tag: str | None = None) -> str:
    """下载 MinerU 结果 zip，解出 full.md 并返回 Markdown 文本。"""
    requests = _requests()
    mnr = cfg.cloud_ocr.mineru
    tag = f" [子任务{task_tag}]" if task_tag else ""
    last_err: Exception | None = None
    for attempt in range(1, mnr.max_retries + 1):
        try:
            resp = requests.get(zip_url, timeout=mnr.timeout)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                md_name = next(
                    (n for n in zf.namelist() if n.endswith("full.md")), None
                )
                if md_name is None:
                    raise RuntimeError(
                        f"MinerU 结果压缩包缺少 full.md: {zf.namelist()[:5]}"
                    )
                text = zf.read(md_name).decode("utf-8", errors="replace")
            log.info(f"MinerU{tag} 结果下载完成: {md_name}（{len(text)} 字符）")
            return text
        except Exception as e:
            last_err = e
            log.warning(f"  MinerU{tag} 结果下载第 {attempt} 次失败: {e}")
        if attempt < mnr.max_retries:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"MinerU 结果下载失败（已重试 {mnr.max_retries} 次）: {last_err}"
    )


def _save_checkpoint_mineru(state_dir: Path, payload: dict):
    payload["kind"] = "mineru"
    tmp = state_dir / "checkpoint.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_dir / "checkpoint.json")


def _mineru_ocr_job(cfg: Config, pdf_path: str | Path,
                    log: logging.Logger,
                    cancel_event: threading.Event | None = None,
                    task_tag: str | None = None) -> str:
    """MinerU 云端任务：申请上传链接 -> 上传 -> 轮询 -> 下载 full.md -> 缓存。"""
    tag = f" [子任务{task_tag}]" if task_tag else ""
    state_dir = _state_dir(cfg, pdf_path)
    _prepare_provider_state(state_dir, "mineru")
    current_total = pdf_page_count(pdf_path)
    cp = _load_checkpoint(state_dir) or {}
    if cp.get("kind") == "mineru" and cp.get("total_pages") == current_total and \
            cp.get("total_units") and len(cp.get("done_units", [])) >= cp["total_units"]:
        log.info("MinerU 任务已完成，直接合并缓存")
        return _merge_parts(state_dir, cp["total_units"], 1)

    mnr = cfg.cloud_ocr.mineru
    last_err: Exception | None = None
    batch_id = cp.get("job_id") if cp.get("kind") == "mineru" else None
    for attempt in range(1, mnr.max_retries + 1):
        if batch_id is None or attempt > 1:
            batch_id, upload_url = _mineru_apply_upload(
                cfg, pdf_path, log, task_tag=task_tag
            )
            _mineru_upload(cfg, pdf_path, upload_url, log)
            _save_checkpoint_mineru(state_dir, {
                "job_id": batch_id, "total_pages": current_total,
                "total_units": 1, "done_units": [],
            })
        try:
            zip_url = _mineru_poll(
                cfg, batch_id, log, cancel_event, task_tag=task_tag
            )
            text = _mineru_download_md(cfg, zip_url, log, task_tag=task_tag)
            break
        except JobCancelledError:
            raise
        except CloudQuotaError:
            raise
        except RuntimeError as e:
            last_err = e
            log.warning(f"  MinerU{tag} 任务第 {attempt} 次失败: {e}")
            batch_id = None
    else:
        raise RuntimeError(
            f"MinerU 云任务失败（已重试 {mnr.max_retries} 次）: {last_err}。"
            "可能原因：PDF 加密/损坏、MinerU 服务繁忙、API Key 失效。"
            "可稍后重试，或换用其他 OCR 引擎（kb ocr mode local）"
        )
    _save_part(state_dir, "0000-0001", text)
    _save_checkpoint_mineru(state_dir, {
        "job_id": batch_id, "total_pages": current_total,
        "total_units": 1, "done_units": [0],
    })
    return _merge_parts(state_dir, 1, 1)


def _mineru_ocr_split_job(cfg: Config, pdf_path: str | Path,
                          log: logging.Logger) -> str:
    """MinerU 云端任务：超过单任务页数上限时自动拆分，识别后按子任务顺序合并。"""
    mnr = cfg.cloud_ocr.mineru
    total = pdf_page_count(pdf_path)
    main_state = _state_dir(cfg, pdf_path)
    cp = _load_checkpoint(main_state) or {}
    if cp.get("kind") == "mineru" and cp.get("total_pages") == total and \
            cp.get("total_units") and len(cp.get("done_units", [])) >= cp["total_units"]:
        log.info("MinerU 任务已完成，直接合并缓存")
        return _merge_parts(main_state, cp["total_units"], 1)
    if total <= mnr.max_pages_per_task:
        return _mineru_ocr_job(cfg, pdf_path, log)

    log.warning(
        f"MinerU 单任务建议不超过 {mnr.max_pages_per_task} 页；"
        f"当前 {total} 页，将拆分为多个子任务，识别完成后自动合并"
    )
    split_dir = main_state / "split"
    ensure_dir(split_dir)
    parts = _split_pdf(pdf_path, split_dir, mnr.max_pages_per_task)
    log.info(f"MinerU 子任务并发数: {mnr.max_workers}")
    texts = _run_split_sub_jobs(
        cfg, parts, _mineru_ocr_job, log, mnr.max_workers, "MinerU"
    )
    return "\n\n".join(t for t in texts if t and t.strip())


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
        json.dumps({"kind": "page", "total": total,
                    "batch_size": batch_size, "done": done},
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

def _cloud_providers(cfg: Config) -> list[str]:
    """云端 provider 链：主 provider + fallback_providers（去重）。"""
    chain = [cfg.cloud_ocr.provider]
    for p in cfg.cloud_ocr.fallback_providers or []:
        if p and p not in chain:
            chain.append(p)
    return chain


def _ocr_page_based(cfg: Config, provider: str, pdf_path: str | Path,
                    log: logging.Logger) -> str:
    """按页/批请求的 provider（openai/baidu）：渲染页面 -> 请求 -> 断点续传。"""
    total = pdf_page_count(pdf_path)
    batch_size = cfg.cloud_ocr.openai.page_batch_size if provider == "openai" else 1
    units = _unit_parts(total, batch_size)
    log.info(
        f"云端 OCR: {Path(pdf_path).name} 共 {total} 页, "
        f"{len(units)} 次请求, provider={provider}"
    )
    state_dir = _state_dir(cfg, pdf_path)
    _prepare_provider_state(state_dir, provider)
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
            images = [
                render_page(pdf_path, i, cfg.cloud_ocr.openai.scale_factor)
                for i in range(s, e)
            ]
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
        raise RuntimeError(
            f"未知云端 OCR provider: {provider}（可选 paddle / mineru / openai / baidu）"
        )
    return _merge_parts(state_dir, total, batch_size)


def _run_cloud_provider(cfg: Config, provider: str, pdf_path: str | Path,
                        log: logging.Logger) -> str:
    if provider == "paddle":
        return _paddle_ocr_split_job(cfg, pdf_path, log)
    if provider == "mineru":
        return _mineru_ocr_split_job(cfg, pdf_path, log)
    if provider in ("openai", "baidu"):
        return _ocr_page_based(cfg, provider, pdf_path, log)
    raise RuntimeError(
        f"未知云端 OCR provider: {provider}（可选 paddle / mineru / openai / baidu）"
    )


def ocr_pdf_cloud(cfg: Config, pdf_path: str | Path, dry_run: bool = False,
                  logger: logging.Logger | None = None) -> str | None:
    """云端 OCR 一个 PDF，返回合并后的 Markdown 文本。

    - 必须通过配置 [cloud_ocr].enabled=true 显式开启（会产生 API 费用）。
    - 按 provider 链处理：主 provider 失败后自动尝试 fallback_providers。
    - 断点续传；中断后再次运行不会重复已完成的页。
    - dry_run 只报告页数与预计请求数，不调用 API。
    """
    log = logger or logging.getLogger("kbimporter")
    if not cfg.cloud_ocr.enabled:
        raise RuntimeError(
            "云端 OCR 未启用：请在配置 [cloud_ocr] enabled=true 显式开启"
            "（风险：付费 API 费用 + 文档图片发送到第三方服务）"
        )
    providers = _cloud_providers(cfg)
    if dry_run:
        try:
            total = pdf_page_count(pdf_path)
        except Exception:
            total = None
        for provider in providers:
            if provider == "paddle":
                if total is not None:
                    pdl = cfg.cloud_ocr.paddle
                    if total > pdl.max_pages_per_task:
                        n_parts = math.ceil(total / pdl.max_pages_per_task)
                        log.info(
                            f"[dry-run] 将拆分 {total} 页为 {n_parts} 个子任务"
                            f"（每任务 ≤{pdl.max_pages_per_task} 页）提交 PaddleOCR（不调用 API）"
                        )
                    else:
                        log.info(
                            f"[dry-run] 将提交整份 PDF（约 {total} 页）到 PaddleOCR"
                            " 云端异步任务（不调用 API）"
                        )
                else:
                    log.info(
                        "[dry-run] 将提交整份 PDF 到 PaddleOCR 云端异步任务（不调用 API）"
                    )
            elif provider == "mineru":
                if total is not None:
                    mnr = cfg.cloud_ocr.mineru
                    if total > mnr.max_pages_per_task:
                        n_parts = math.ceil(total / mnr.max_pages_per_task)
                        log.info(
                            f"[dry-run] 将拆分 {total} 页为 {n_parts} 个子任务"
                            f"（每任务 ≤{mnr.max_pages_per_task} 页）提交 MinerU（不调用 API）"
                        )
                    else:
                        log.info(
                            f"[dry-run] 将上传整份 PDF（约 {total} 页）到 MinerU"
                            " 云端异步任务（不调用 API）"
                        )
                else:
                    log.info(
                        "[dry-run] 将上传整份 PDF 到 MinerU 云端异步任务（不调用 API）"
                    )
            else:
                batch_size = (
                    cfg.cloud_ocr.openai.page_batch_size
                    if provider == "openai" else 1
                )
                units = _unit_parts(total, batch_size) if total else []
                log.info(
                    f"[dry-run] provider={provider} 预计 {len(units)} 次 API 请求"
                    "（不会调用）"
                )
        return None
    log.warning("云端 OCR 将调用付费 API，并把文档图片发送到第三方服务（配置已显式开启）")

    errors: list[str] = []
    for provider in providers:
        if _quota_exhausted(provider):
            log.info(f"云端 OCR provider {provider} 今日额度已用完，本次运行跳过")
            errors.append(f"{provider}: 今日额度已用完（本次运行跳过）")
            continue
        try:
            return _run_cloud_provider(cfg, provider, pdf_path, log)
        except CloudQuotaError as e:
            _mark_quota_exhausted(provider)
            errors.append(f"{provider}: {e}")
            log.warning(
                f"云端 OCR provider {provider} 当日额度已用完，立即切换到下一通道: {e}"
            )
        except Exception as e:
            errors.append(f"{provider}: {e}")
            log.warning(f"云端 OCR provider {provider} 失败，尝试下一个: {e}")
    raise RuntimeError("云端 OCR 全部失败: " + " | ".join(errors))


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
