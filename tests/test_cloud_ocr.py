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

    def fake_submit(cfg_, pdf, log):
        calls["submit"] += 1
        return "job1"

    def fake_poll(cfg_, job_id, log):
        return {"resultUrl": {"jsonUrl": "http://example.com/result.jsonl"}}

    def fake_download(cfg_, data, log):
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

    def fake_submit(cfg_, pdf, log):
        calls["submit"] += 1
        return f"job{calls['submit']}"

    def fake_poll(cfg_, job_id, log):
        calls["poll"] += 1
        if calls["poll"] == 1:
            raise RuntimeError("PaddleOCR 云任务失败: OCR服务请求失败，状态码 500")
        return {"resultUrl": {"jsonUrl": "http://example.com/result.jsonl"}}

    def fake_download(cfg_, data, log):
        return ["第一页内容", "第二页内容"]

    monkeypatch.setattr(cloud_ocr, "_paddle_submit", fake_submit)
    monkeypatch.setattr(cloud_ocr, "_paddle_poll", fake_poll)
    monkeypatch.setattr(cloud_ocr, "_paddle_download_pages", fake_download)

    text = cloud_ocr.ocr_pdf_cloud(cfg, "doc.pdf")
    assert "第一页内容" in text and "第二页内容" in text
    assert calls["submit"] == 2
    assert calls["poll"] == 2


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
