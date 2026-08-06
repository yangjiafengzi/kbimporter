from __future__ import annotations

from kbimporter.config import ChunkConfig


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _slice_to_byte_limit(text: str, byte_limit: int) -> str:
    result: list[str] = []
    total = 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if total + b > byte_limit:
            break
        result.append(ch)
        total += b
    return "".join(result)


def _find_split_point(text: str, max_bytes: int, separators: list[str]) -> int:
    safe_max = max_bytes - 1
    blen = _byte_len(text)
    if blen <= safe_max:
        return len(text)

    search_start_char = 0
    byte_count = 0
    for i, ch in enumerate(text):
        byte_count += len(ch.encode("utf-8"))
        if byte_count >= int(safe_max * 0.6):
            search_start_char = i
            break

    window = text[search_start_char:]
    best = -1
    for sep in separators:
        pos = window.rfind(sep)
        if pos == -1:
            continue
        candidate_text = window[: pos + len(sep)]
        candidate_bytes = _byte_len(text[: search_start_char + pos + len(sep)])
        if candidate_bytes <= safe_max:
            char_pos = search_start_char + pos + len(sep)
            if best == -1 or char_pos > best:
                best = char_pos

    if best > 0:
        return best

    byte_pos = 0
    for i, ch in enumerate(text):
        byte_pos += len(ch.encode("utf-8"))
        if byte_pos >= safe_max:
            return i
    return len(text)


def _split_at_boundary(text: str, max_bytes: int, overlap: int,
                       separators: list[str]) -> list[str]:
    if not text.strip():
        return []
    hard_limit = max_bytes - 2
    chunks: list[str] = []
    start = 0
    safety = 0
    while start < len(text) and safety < 100000:
        safety += 1
        remaining = text[start:]
        if _byte_len(remaining) <= hard_limit:
            chunk = _slice_to_byte_limit(remaining, hard_limit).strip()
            if chunk:
                chunks.append(chunk)
            break
        cut = _find_split_point(remaining, hard_limit, separators)
        if cut <= 0:
            cut = 1
            while _byte_len(remaining[: cut + 1]) <= hard_limit and cut < len(remaining):
                cut += 1
        chunk = remaining[:cut].strip()
        if _byte_len(chunk) > hard_limit:
            chunk = _slice_to_byte_limit(chunk, hard_limit).strip()
        if chunk:
            chunks.append(chunk)

        overlap_char = 0
        byte_count = 0
        for ch in reversed(remaining[:cut]):
            byte_count += len(ch.encode("utf-8"))
            if byte_count > overlap:
                break
            overlap_char += 1
        start += max(cut - overlap_char, 1)

    return [c for c in chunks if c.strip()]


def chunk_document(text: str, cfg: ChunkConfig | None = None) -> tuple[list[str], list[str], list[int]]:
    """双层切片：粗块（默认 8KB）+ 细块（默认 1KB），返回 (粗块, 细块, 细块所属粗块序号)。"""
    cfg = cfg or ChunkConfig()
    coarse_chunks = _split_at_boundary(
        text, cfg.coarse_size, cfg.coarse_overlap, cfg.separators
    )
    if not coarse_chunks:
        return [], [], []

    fine_chunks: list[str] = []
    parent_indices: list[int] = []
    for i, coarse in enumerate(coarse_chunks):
        fines = _split_at_boundary(
            coarse, cfg.fine_size, cfg.fine_overlap, cfg.separators
        )
        for _ in fines:
            parent_indices.append(i)
        fine_chunks.extend(fines)

    return coarse_chunks, fine_chunks, parent_indices

