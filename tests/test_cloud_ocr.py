from __future__ import annotations

import logging
import requests
from pathlib import Path

import pytest

from kbimporter import cloud_ocr


def _enable_cloud(cfg, provider="openai"):
    cfg.cloud_ocr.enabled = True
    cfg.cloud_ocr.provider = provider
    cfg.cloud_ocr.state_dir = cfg.state_dir / "cloud_ocr"
    return cfg


def test_disabled_raises(cfg):
    with pytest.raises(RuntimeError, match="未启用"):
        cloud_ocr.ocr_pdf_cloud(cfg, "x.pdf")


def test_dry_run_reports_without_api(cfg, monkeypatch):
    _enable_cloud(cfg)
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 5)
    monkeypatch.setattr(cloud_ocr, "render_page", lambda p, i, s: b"png")
    monkeypatch.setattr(cloud_ocr, "_openai_batch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 API")))
    result = cloud_ocr.ocr_pdf_cloud(cfg, "x.pdf", dry_run=True)
    assert result is None


def test_openai_provider_with_checkpoint_resume(cfg, monkeypatch):
    _enable_cloud(cfg)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    calls = []
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 3)
    monkeypatch.setattr(cloud_ocr, "render_page", lambda p, i, s: f"img{i}".encode())

    def fake_batch(cfg_, api_key, images, log):
        calls.append(len(images))
        return f"第{len(calls)}批文本"

    monkeypatch.setattr(cloud_ocr, "_openai_batch", fake_batch)
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "第1批文本" in text and "第3批文本" in text
    assert len(calls) == 3

    # 再次运行：断点已存在，不应再调用 API
    text2 = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text2 == text
    assert len(calls) == 3


def test_baidu_provider(cfg, monkeypatch):
    _enable_cloud(cfg, provider="baidu")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    monkeypatch.setattr(cloud_ocr, "render_page", lambda p, i, s: b"img")
    monkeypatch.setattr(cloud_ocr, "_baidu_token", lambda c, l: "token")
    monkeypatch.setattr(cloud_ocr, "_baidu_page", lambda c, t, img, l: "百度识别文本")
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "百度识别文本" in text


def test_paddle_provider_job_flow_with_resume(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"submit": 0}

    def fake_submit(cfg_, pdf, log, task_tag=None):
        calls["submit"] += 1
        return "job1"

    def fake_poll(cfg_, job_id, log, cancel_event=None, task_tag=None):
        return {"resultUrl": {"jsonUrl": "http://example.com/result.jsonl"}}

    def fake_download(cfg_, data, log, task_tag=None):
        return ["第一页内容", "第二页内容"]

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(cloud_ocr, "_paddle_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_paddle_download_pages", fake_download)

    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "第一页内容" in text and "第二页内容" in text
    text2 = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text2 == text
    assert calls["submit"] == 1  # 断点续传：不重复提交任务


def test_paddle_submit_retries_on_500(cfg, monkeypatch, tmp_path: Path):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"pdf")
    calls = {"n": 0}

    class Resp:
        status_code = 500
        text = "server error"

        def json(self):
            raise AssertionError("不应解析 JSON")

    def fake_post(*a, **k):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="已重试"):
        cloud_ocr._paddle_submit(cfg, pdf, logging.getLogger("t"))
    assert calls["n"] == 3  # 默认 max_retries=3


def test_paddle_provider_retries_failed_job(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"submit": 0, "poll": 0}

    def fake_submit(cfg_, pdf, log, task_tag=None):
        calls["submit"] += 1
        return f"job{calls['submit']}"

    def fake_poll(cfg_, job_id, log, cancel_event=None, task_tag=None):
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise RuntimeError("PaddleOCR 云任务失败: OCR服务请求失败，状态码 500")
        return {"resultUrl": {"jsonUrl": "http://example.com/result.jsonl"}}

    def fake_download(cfg_, data, log, task_tag=None):
        return ["第一页内容", "第二页内容"]

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(cloud_ocr, "_paddle_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_paddle_download_pages", fake_download)

    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "第一页内容" in text and "第二页内容" in text
    assert calls["submit"] == 2
    assert calls["poll"] == 2


def test_split_pdf_splits_by_pages(tmp_path: Path):
    import fitz
    src = tmp_path / "book.pdf"
    doc = fitz.open()
    for i in range(5):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i + 1}")
    doc.save(str(src))
    doc.close()
    out = tmp_path / "split"
    out.mkdir()
    parts = cloud_ocr._split_pdf(src, out, 2)
    assert len(parts) == 3
    sizes = []
    for p in parts:
        d = fitz.open(str(p))
        sizes.append(len(d))
        d.close()
    assert sizes == [2, 2, 1]


def test_paddle_split_job_merges_parts(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.max_pages_per_task = 100
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 250)
    monkeypatch.setattr(
        cloud_ocr, "_split_pdf",
        lambda pdf, out, maxp: [Path("a.pdf"), Path("b.pdf"), Path("c.pdf")],
    )
    calls: list[str] = []

    def fake_job(cfg_, p, log, cancel_event=None, task_tag=None):
        calls.append(p.name)
        return f"part{['a.pdf', 'b.pdf', 'c.pdf'].index(p.name) + 1}"

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_job", fake_job)
    text = cloud_ocr._paddle_ocr_split_job(cfg, "big.pdf", logging.getLogger("t"))
    assert text == "part1\n\npart2\n\npart3"
    assert sorted(calls) == ["a.pdf", "b.pdf", "c.pdf"]


def test_paddle_split_job_reuses_completed_checkpoint(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    state_dir = cloud_ocr._state_dir(cfg, "big.pdf")
    cloud_ocr._save_part(state_dir, "0000-0001", "page1")
    cloud_ocr._save_part(state_dir, "0001-0002", "page2")
    cloud_ocr._save_checkpoint_paddle(
        state_dir, {"job_id": "x", "total_pages": 2, "done_pages": [0, 1]}
    )
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)

    def no_split(*a):
        raise AssertionError("不应拆分已完成的缓存")

    monkeypatch.setattr(cloud_ocr, "_split_pdf", no_split)
    text = cloud_ocr._paddle_ocr_split_job(cfg, "big.pdf", logging.getLogger("t"))
    assert text == "page1\n\npage2"


def test_paddle_job_resets_checkpoint_when_pages_change(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    state_dir = cloud_ocr._state_dir(cfg, "doc.pdf")
    cloud_ocr._save_checkpoint_paddle(
        state_dir, {"job_id": "old", "total_pages": 3, "done_pages": [0, 1, 2]}
    )
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 5)
    calls = {"submit": 0}

    def fake_submit(cfg_, pdf, log, task_tag=None):
        calls["submit"] += 1
        return "job1"

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(
        cloud_ocr, "_paddle_poll",
        lambda c, j, l, cancel_event=None, task_tag=None: {"resultUrl": {"jsonUrl": "u"}},
    )
    monkeypatch.setattr(
        cloud_ocr, "_paddle_download_pages",
        lambda c, d, l, task_tag=None: [f"p{i}" for i in range(5)],
    )
    text = cloud_ocr._paddle_ocr_job(cfg, "doc.pdf", logging.getLogger("t"))
    assert calls["submit"] == 1
    assert text == "p0\n\np1\n\np2\n\np3\n\np4"


def test_paddle_dry_run_reports_split(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 250)
    assert cloud_ocr.ocr_pdf_cloud(cfg, "big.pdf", dry_run=True) is None


def test_baidu_token_requires_keys(cfg, monkeypatch):
    _enable_cloud(cfg, provider="baidu")
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="百度 OCR 缺少密钥"):
        cloud_ocr._baidu_token(cfg, logging.getLogger("t"))


def test_write_cloud_ocr_md(cfg, monkeypatch, tmp_path: Path):
    _enable_cloud(cfg)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 1)
    monkeypatch.setattr(cloud_ocr, "render_page", lambda p, i, s: b"img")
    monkeypatch.setattr(cloud_ocr, "_openai_batch", lambda *a, **k: "结果文本")
    dest = tmp_path / "out.md"
    assert cloud_ocr.write_cloud_ocr_md(cfg, "doc.pdf", dest) is True
    assert dest.read_text(encoding="utf-8") == "结果文本"


def test_mineru_provider_job_flow_with_resume(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"apply": 0, "upload": 0, "poll": 0, "download": 0}

    def fake_apply(cfg_, pdf, log, task_tag=None):
        calls["apply"] += 1
        return "batch1", "http://example.com/upload"

    def fake_upload(cfg_, pdf, url, log):
        calls["upload"] += 1
        assert url == "http://example.com/upload"

    def fake_poll(cfg_, batch_id, log, cancel_event=None, task_tag=None):
        calls["poll"] += 1
        return "http://example.com/result.zip"

    def fake_download(cfg_, zip_url, log, task_tag=None):
        calls["download"] += 1
        return "MinerU 识别全文"

    monkeypatch.setattr(cloud_ocr, "_mineru_apply_upload", fake_apply)
    monkeypatch.setattr(cloud_ocr, "_mineru_upload", fake_upload)
    monkeypatch.setattr(cloud_ocr, "_mineru_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_mineru_download_md", fake_download)

    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "MinerU 识别全文" in text
    text2 = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text2 == text
    assert calls == {"apply": 1, "upload": 1, "poll": 1, "download": 1}


def test_fallback_from_paddle_to_mineru(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    cfg.cloud_ocr.fallback_providers = ["mineru"]
    calls = {"paddle": 0, "mineru": 0}

    def fake_paddle(cfg_, pdf, log):
        calls["paddle"] += 1
        raise RuntimeError("PaddleOCR 云任务失败")

    def fake_mineru(cfg_, pdf, log):
        calls["mineru"] += 1
        return "mineru 结果"

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_split_job", fake_paddle)
    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_split_job", fake_mineru)
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text == "mineru 结果"
    assert calls == {"paddle": 1, "mineru": 1}


def test_all_providers_failed_raises_combined_error(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    cfg.cloud_ocr.fallback_providers = ["mineru"]

    def fake_paddle(cfg_, pdf, log):
        raise RuntimeError("paddle 挂了")

    def fake_mineru(cfg_, pdf, log):
        raise RuntimeError("mineru 挂了")

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_split_job", fake_paddle)
    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_split_job", fake_mineru)
    with pytest.raises(RuntimeError, match="全部失败") as exc:
        cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "paddle: paddle 挂了" in str(exc.value)
    assert "mineru: mineru 挂了" in str(exc.value)


def test_mineru_apply_retries_on_500(cfg, monkeypatch, tmp_path: Path):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"pdf")
    calls = {"n": 0}

    class Resp:
        status_code = 500
        text = "server error"

        def json(self):
            raise AssertionError("不应解析 JSON")

    def fake_post(*a, **k):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="已重试"):
        cloud_ocr._mineru_apply_upload(cfg, pdf, logging.getLogger("t"))
    assert calls["n"] == 3


def test_mineru_provider_retries_failed_job(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"apply": 0, "poll": 0}

    def fake_apply(cfg_, pdf, log, task_tag=None):
        calls["apply"] += 1
        return f"batch{calls['apply']}", f"http://example.com/upload{calls['apply']}"

    def fake_upload(cfg_, pdf, url, log):
        pass

    def fake_poll(cfg_, batch_id, log, cancel_event=None, task_tag=None):
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise RuntimeError("MinerU 云任务失败: 服务繁忙")
        return "http://example.com/result.zip"

    def fake_download(cfg_, zip_url, log, task_tag=None):
        return "mineru 全文"

    monkeypatch.setattr(cloud_ocr, "_mineru_apply_upload", fake_apply)
    monkeypatch.setattr(cloud_ocr, "_mineru_upload", fake_upload)
    monkeypatch.setattr(cloud_ocr, "_mineru_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_mineru_download_md", fake_download)

    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text == "mineru 全文"
    assert calls == {"apply": 2, "poll": 2}


def test_mineru_split_job_merges_parts(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    cfg.cloud_ocr.mineru.max_pages_per_task = 100
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 250)
    monkeypatch.setattr(
        cloud_ocr, "_split_pdf",
        lambda pdf, out, maxp: [Path("a.pdf"), Path("b.pdf"), Path("c.pdf")],
    )
    calls: list[str] = []

    def fake_job(cfg_, p, log, cancel_event=None, task_tag=None):
        calls.append(p.name)
        return f"part{['a.pdf', 'b.pdf', 'c.pdf'].index(p.name) + 1}"

    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_job", fake_job)
    text = cloud_ocr._mineru_ocr_split_job(cfg, "big.pdf", logging.getLogger("t"))
    assert text == "part1\n\npart2\n\npart3"
    assert sorted(calls) == ["a.pdf", "b.pdf", "c.pdf"]


def test_mineru_job_reuses_completed_checkpoint(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    state_dir = cloud_ocr._state_dir(cfg, "doc.pdf")
    cloud_ocr._save_part(state_dir, "0000-0001", "cached md")
    cloud_ocr._save_checkpoint_mineru(
        state_dir,
        {"job_id": "x", "total_pages": 2, "total_units": 1, "done_units": [0]},
    )
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)

    def no_submit(*a, **k):
        raise AssertionError("不应重新提交")

    monkeypatch.setattr(cloud_ocr, "_mineru_apply_upload", no_submit)
    text = cloud_ocr._mineru_ocr_job(cfg, "doc.pdf", logging.getLogger("t"))
    assert text == "cached md"


def test_mineru_requires_key(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="缺少 MinerU 云 API 密钥"):
        cloud_ocr._mineru_headers(cfg)


def test_mineru_dry_run_reports_without_api(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 150)
    assert cloud_ocr.ocr_pdf_cloud(cfg, "big.pdf", dry_run=True) is None


def test_provider_state_isolation_clears_old_parts(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    state_dir = cloud_ocr._state_dir(cfg, "doc.pdf")
    cloud_ocr._save_part(state_dir, "0000-0001", "page 1")
    cloud_ocr._save_checkpoint(state_dir, 1, 1, ["0000-0001"])
    cloud_ocr._prepare_provider_state(state_dir, "mineru")
    assert cloud_ocr._load_checkpoint(state_dir) is None
    assert not (state_dir / "parts").exists()


def test_looks_like_quota_error():
    assert cloud_ocr._looks_like_quota_error(429) is True
    assert cloud_ocr._looks_like_quota_error(None, "daily quota reached") is False
    assert cloud_ocr._looks_like_quota_error(None, "额度已用完") is False
    assert cloud_ocr._looks_like_quota_error(None, "60018") is False
    assert cloud_ocr._looks_like_quota_error(
        None, "60018", quota_codes=("60018",)
    ) is True
    assert cloud_ocr._looks_like_quota_error(None, "今日解析量已达上限") is False
    assert cloud_ocr._looks_like_quota_error(None, "file size exceeds limit") is False
    assert cloud_ocr._looks_like_quota_error(None, "超出文件大小上限") is False
    assert cloud_ocr._looks_like_quota_error(503, "server busy") is False


def test_quota_skip_paddle_for_rest_of_run(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    cfg.cloud_ocr.fallback_providers = ["mineru"]
    calls = {"paddle": 0, "mineru": 0}

    def fake_paddle(cfg_, pdf, log):
        calls["paddle"] += 1
        raise cloud_ocr.CloudQuotaError("PaddleOCR 当日解析额度已用完")

    def fake_mineru(cfg_, pdf, log):
        calls["mineru"] += 1
        return "mineru result"

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_split_job", fake_paddle)
    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_split_job", fake_mineru)
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc1.pdf")
    assert text == "mineru result"
    assert calls == {"paddle": 1, "mineru": 1}

    text2 = cloud_ocr.ocr_pdf_cloud(cfg, "doc2.pdf")
    assert text2 == "mineru result"
    assert calls == {"paddle": 1, "mineru": 2}  # 本次运行剩余文件不再尝试 paddle


def test_paddle_poll_http_500_raises_retryable_runtime_error(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.max_poll_seconds = 10

    class Resp:
        status_code = 500
        text = "server error"

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="HTTP 500"):
        cloud_ocr._paddle_poll(cfg, "job1", logging.getLogger("t"))


def test_mineru_poll_http_500_raises_retryable_runtime_error(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    cfg.cloud_ocr.mineru.max_poll_seconds = 10

    class Resp:
        status_code = 500
        text = "server error"

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="HTTP 500"):
        cloud_ocr._mineru_poll(cfg, "batch1", logging.getLogger("t"))


def test_paddle_http_error_retries_without_immediate_failover(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    cfg.cloud_ocr.fallback_providers = ["mineru"]
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"submit": 0, "mineru": 0}

    def fake_submit(cfg_, pdf, log, task_tag=None):
        calls["submit"] += 1
        return "job1"

    def fake_poll(cfg_, job_id, log, cancel_event=None, task_tag=None):
        if calls["submit"] == 1:
            raise RuntimeError("PaddleOCR 轮询请求失败: HTTP 500")
        return {"resultUrl": {"jsonUrl": "http://example.com/result.jsonl"}}

    def fake_download(cfg_, data, log, task_tag=None):
        return ["第一页内容", "第二页内容"]

    def fake_mineru(cfg_, pdf, log):
        calls["mineru"] += 1
        return "mineru result"

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(cloud_ocr, "_paddle_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_paddle_download_pages", fake_download)
    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_split_job", fake_mineru)
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert calls["submit"] == 2  # HTTP 500 会重新提交重试，而不是立即切换
    assert calls["mineru"] == 0
    assert "第一页内容" in text


def test_paddle_submit_quota_error_fails_fast(cfg, monkeypatch, tmp_path: Path):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"pdf")
    calls = {"n": 0}

    class Resp:
        status_code = 429
        text = "daily limit reached"

        def json(self):
            raise AssertionError("不应解析 JSON")

    def fake_post(*a, **k):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cloud_ocr.CloudQuotaError, match="额度"):
        cloud_ocr._paddle_submit(cfg, pdf, logging.getLogger("t"))
    assert calls["n"] == 1  # 429 不重试，立即熔断


def test_paddle_poll_quota_error_fails_fast(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.max_poll_seconds = 10

    class Resp:
        status_code = 429
        text = "quota"

        def raise_for_status(self):
            pass

        def json(self):
            raise AssertionError("不应解析 JSON")

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    with pytest.raises(cloud_ocr.CloudQuotaError, match="额度"):
        cloud_ocr._paddle_poll(cfg, "job1", logging.getLogger("t"))


def test_paddle_ocr_job_quota_no_resubmit(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 2)
    calls = {"submit": 0}

    def fake_submit(cfg_, pdf, log, task_tag=None):
        calls["submit"] += 1
        return "job1"

    def fake_poll(cfg_, job_id, log, cancel_event=None, task_tag=None):
        raise cloud_ocr.CloudQuotaError("PaddleOCR 当日解析额度已用完")

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(cloud_ocr, "_paddle_poll", fake_poll)
    with pytest.raises(cloud_ocr.CloudQuotaError):
        cloud_ocr._paddle_ocr_job(cfg, "doc.pdf", logging.getLogger("t"))
    assert calls["submit"] == 1  # 额度错误不重新提交


def test_fallback_to_mineru_on_paddle_quota(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    cfg.cloud_ocr.fallback_providers = ["mineru"]
    calls = {"paddle": 0, "mineru": 0}

    def fake_paddle(cfg_, pdf, log):
        calls["paddle"] += 1
        raise cloud_ocr.CloudQuotaError("PaddleOCR 当日解析额度已用完")

    def fake_mineru(cfg_, pdf, log):
        calls["mineru"] += 1
        return "mineru result"

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_split_job", fake_paddle)
    monkeypatch.setattr(cloud_ocr, "_mineru_ocr_split_job", fake_mineru)
    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert text == "mineru result"
    assert calls == {"paddle": 1, "mineru": 1}


def test_paddle_split_job_runs_subtasks_concurrently(cfg, monkeypatch):
    import threading

    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.max_pages_per_task = 100
    cfg.cloud_ocr.paddle.max_workers = 2
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 250)
    monkeypatch.setattr(
        cloud_ocr, "_split_pdf",
        lambda pdf, out, maxp: [Path("a.pdf"), Path("b.pdf")],
    )
    threads: list[str] = []
    barrier = threading.Barrier(2)

    def fake_job(cfg_, p, log, cancel_event=None, task_tag=None):
        threads.append(threading.current_thread().name)
        barrier.wait(timeout=5)
        return f"part-{p.stem}"

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_job", fake_job)
    text = cloud_ocr._paddle_ocr_split_job(cfg, "big.pdf", logging.getLogger("t"))
    assert "part-a" in text and "part-b" in text
    assert len(set(threads)) >= 2


def test_paddle_split_job_stops_on_quota(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.max_pages_per_task = 100
    cfg.cloud_ocr.paddle.max_workers = 2
    monkeypatch.setattr(cloud_ocr, "pdf_page_count", lambda p: 250)
    monkeypatch.setattr(
        cloud_ocr, "_split_pdf",
        lambda pdf, out, maxp: [Path("a.pdf"), Path("b.pdf"), Path("c.pdf")],
    )
    calls: list[str] = []

    def fake_job(cfg_, p, log, cancel_event=None, task_tag=None):
        calls.append(p.name)
        raise cloud_ocr.CloudQuotaError("PaddleOCR 当日解析额度已用完")

    monkeypatch.setattr(cloud_ocr, "_paddle_ocr_job", fake_job)
    with pytest.raises(cloud_ocr.CloudQuotaError):
        cloud_ocr._paddle_ocr_split_job(cfg, "big.pdf", logging.getLogger("t"))
    assert len(calls) < 3 and "c.pdf" not in calls  # 后续波次不再提交


def test_paddle_poll_stall_timeout_raises(cfg, monkeypatch):
    _enable_cloud(cfg, provider="paddle")
    monkeypatch.setenv("PADDLE_OCR_API_KEY", "token")
    cfg.cloud_ocr.paddle.stall_timeout = 1
    cfg.cloud_ocr.paddle.poll_interval = 1
    cfg.cloud_ocr.paddle.max_poll_seconds = 30
    clock = {"t": 1000.0}
    monkeypatch.setattr(cloud_ocr.time, "time", lambda: clock["t"])
    monkeypatch.setattr(cloud_ocr.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    class Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "state": "running",
                    "extractProgress": {"extractedPages": 0, "totalPages": 2},
                }
            }

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="停滞"):
        cloud_ocr._paddle_poll(cfg, "job1", logging.getLogger("t"))


def test_mineru_apply_upload_quota_error_fails_fast(cfg, monkeypatch, tmp_path: Path):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"pdf")
    calls = {"n": 0}

    class Resp:
        status_code = 429
        text = "quota exceeded"

        def json(self):
            raise AssertionError("不应解析 JSON")

    def fake_post(*a, **k):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(cloud_ocr.CloudQuotaError):
        cloud_ocr._mineru_apply_upload(cfg, pdf, logging.getLogger("t"))
    assert calls["n"] == 1


def test_mineru_poll_quota_error_on_failed_state(cfg, monkeypatch):
    _enable_cloud(cfg, provider="mineru")
    monkeypatch.setenv("MINERU_API_KEY", "sk-test")
    cfg.cloud_ocr.mineru.max_poll_seconds = 10

    class Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "extract_result": [
                        {"state": "failed", "err_msg": "每日额度已用完 -60018"}
                    ]
                }
            }

    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    with pytest.raises(cloud_ocr.CloudQuotaError, match="额度"):
        cloud_ocr._mineru_poll(cfg, "batch1", logging.getLogger("t"))
