from __future__ import annotations

import json
from pathlib import Path

from kbimporter.zotero_sync import (
    calc_chinese_ratio,
    detect_zotero_storage,
    get_base_name,
    load_hash_history,
    pdf_has_text_layer,
    read_zotero_data_dir,
    save_hash_history,
    sync_zotero,
)


def _make_pdf(path: Path, text: str = ""):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


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


def test_read_zotero_data_dir_from_prefs(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "Profiles"
    (profiles / "abc.default").mkdir(parents=True)
    (profiles / "abc.default" / "prefs.js").write_text(
        'user_pref("extensions.zotero.dataDir", "/Users/Shared/zotero");\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "kbimporter.zotero_sync._zotero_profiles_dirs", lambda: [profiles]
    )
    assert read_zotero_data_dir() == Path("/Users/Shared/zotero")
    detected, custom = detect_zotero_storage()
    assert detected == Path("/Users/Shared/zotero") / "storage"
    assert custom is True


def test_read_zotero_data_dir_windows_escaped_path(tmp_path: Path, monkeypatch):
    profiles = tmp_path / "Profiles"
    (profiles / "p").mkdir(parents=True)
    value = r"C:\Users\x\Zotero"
    escaped = value.replace("\\", "\\\\")
    (profiles / "p" / "prefs.js").write_text(
        f'user_pref("extensions.zotero.dataDir", "{escaped}");\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "kbimporter.zotero_sync._zotero_profiles_dirs", lambda: [profiles]
    )
    assert read_zotero_data_dir() == Path(r"C:\Users\x\Zotero")


def test_detect_zotero_storage_default_fallback(monkeypatch):
    monkeypatch.setattr("kbimporter.zotero_sync.read_zotero_data_dir", lambda: None)
    detected, custom = detect_zotero_storage()
    assert detected == Path.home() / "Zotero" / "storage"
    assert custom is False


def test_pdf_text_layer_detection(tmp_path: Path):
    text_pdf = tmp_path / "text.pdf"
    _make_pdf(text_pdf, "English original")
    scan_pdf = tmp_path / "scan.pdf"
    _make_pdf(scan_pdf, "")
    assert pdf_has_text_layer(text_pdf) is True
    assert pdf_has_text_layer(scan_pdf) is False


def test_sync_prefers_text_layer_over_scan(cfg: Config, tmp_path: Path):
    src = cfg.zotero_storage
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    text_pdf = src / "a" / "Author - 2020 - Title.pdf"
    _make_pdf(text_pdf, "English original content")
    scan_pdf = src / "b" / "Author - 2020 - Title.pdf"
    _make_pdf(scan_pdf, "")

    stats = sync_zotero(cfg, dry_run=False)
    assert stats["copied"] == 1
    assert stats["no_text_layer"] == 1
    copied = cfg.library_dir / "Author - 2020 - Title.pdf"
    assert copied.exists()
    assert copied.read_bytes() == text_pdf.read_bytes()
    saved = load_hash_history(cfg.hash_history_file)
    best = next(r for r in saved.values() if r["copied"])
    assert best["has_text"] is True
