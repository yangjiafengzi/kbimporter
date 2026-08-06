from __future__ import annotations

import json
from pathlib import Path

from kbimporter.zotero_sync import (
    calc_chinese_ratio,
    get_base_name,
    load_hash_history,
    save_hash_history,
    sync_zotero,
)


def test_helpers():
    assert get_base_name("a.b.pdf") == "a.b"
    assert calc_chinese_ratio("中文内容abc") == 4 / 7
    assert calc_chinese_ratio("") == 0


def _hash(path: Path):
    from kbimporter.util import sha256_file
    return sha256_file(path)


def test_sync_copies_best_version_and_cleans_stale(cfg: Config, tmp_path: Path):
    src = cfg.zotero_storage
    src.mkdir(parents=True)
    f1 = src / "Author - 2020 - Title.pdf"
    f2 = src / "Author - 2020 - Title.epub"
    f1.write_bytes(b"english original content")
    f2.write_bytes("中文译本内容".encode("utf-8"))
    h1, h2 = _hash(f1), _hash(f2)
    # 预置历史：f1 是原版（低中文比例），f2 是译本；另有一条过期记录指向已复制的文件
    stale_pdf = cfg.library_dir / "Stale - 1999 - Old.pdf"
    stale_pdf.write_bytes(b"stale")
    history = {
        h1: {"path": str(f1), "chinese_ratio": 0.0, "copied": False},
        h2: {"path": str(f2), "chinese_ratio": 0.8, "copied": False},
        "stalehash": {"path": str(stale_pdf), "chinese_ratio": 0.1, "copied": True},
    }
    save_hash_history(cfg.hash_history_file, history)

    stats = sync_zotero(cfg, dry_run=False)
    assert stats["copied"] == 1
    assert (cfg.library_dir / "Author - 2020 - Title.pdf").exists()
    assert not (cfg.library_dir / "Author - 2020 - Title.epub").exists()
    assert not stale_pdf.exists()
    saved = load_hash_history(cfg.hash_history_file)
    assert saved[h1]["copied"] is True
    assert "stalehash" not in saved
    assert stats["cleaned"] == 1


def test_sync_dry_run_writes_nothing(cfg: Config):
    src = cfg.zotero_storage
    src.mkdir(parents=True)
    f = src / "Author - 2020 - Title.pdf"
    f.write_bytes(b"data")
    stats = sync_zotero(cfg, dry_run=True)
    assert stats["copied"] == 1
    assert not (cfg.library_dir / "Author - 2020 - Title.pdf").exists()
    assert not cfg.hash_history_file.exists()


def test_sync_skips_when_already_best(cfg: Config):
    src = cfg.zotero_storage
    src.mkdir(parents=True)
    f = src / "Author - 2020 - Title.pdf"
    f.write_bytes(b"data")
    h = _hash(f)
    (cfg.library_dir / "Author - 2020 - Title.md").write_text("md", encoding="utf-8")
    save_hash_history(cfg.hash_history_file, {
        h: {"path": str(f), "chinese_ratio": 0.0, "copied": True},
    })
    stats = sync_zotero(cfg, dry_run=False)
    assert stats["skipped"] == 1
    assert stats["copied"] == 0
