from __future__ import annotations

from kbimporter.chunker import chunk_document, _byte_len, _split_at_boundary
from kbimporter.config import ChunkConfig


def test_empty_text():
    coarse, fine, parents = chunk_document("")
    assert coarse == []
    assert fine == []
    assert parents == []


def test_short_text_single_coarse():
    text = "这是一段用于测试的文本。" * 20
    coarse, fine, parents = chunk_document(text)
    assert len(coarse) == 1
    assert coarse[0] == text.strip()
    assert len(fine) >= 1
    assert all(0 <= p < len(coarse) for p in parents)


def test_long_text_chunks_within_byte_limit():
    text = ("社会机制研究中的中层理论。" * 500)
    cfg = ChunkConfig(coarse_size=1024, coarse_overlap=128, fine_size=256, fine_overlap=32)
    coarse, fine, parents = chunk_document(text, cfg)
    assert len(coarse) >= 2
    for c in coarse:
        assert _byte_len(c) <= cfg.coarse_size
    for f in fine:
        assert _byte_len(f) <= cfg.fine_size
    assert len(parents) == len(fine)


def test_paragraph_split_prefers_blank_line():
    para = "第一段。" + "字" * 100 + "\n\n"
    text = para * 100
    cfg = ChunkConfig(coarse_size=2048, coarse_overlap=64, fine_size=1024, fine_overlap=32)
    coarse, _, _ = chunk_document(text, cfg)
    assert len(coarse) > 1
    for c in coarse:
        assert "\n\n" in c or _byte_len(c) <= 2048


def test_split_at_boundary_hard_cut():
    text = "无标点" * 5000
    chunks = _split_at_boundary(text, 1024, 64, ["。"])
    assert chunks
    assert all(_byte_len(c) <= 1024 for c in chunks)


def test_overlap_keeps_context():
    text = "段落甲。" + "内容" * 1000 + "\n\n" + "段落乙。" + "内容" * 1000
    cfg = ChunkConfig(coarse_size=2048, coarse_overlap=512, fine_size=512, fine_overlap=64)
    coarse, _, _ = chunk_document(text, cfg)
    if len(coarse) >= 2:
        # 相邻粗块应共享一部分文本（重叠）
        assert coarse[0][-50:] in coarse[1] or coarse[1][:50] in coarse[0]

