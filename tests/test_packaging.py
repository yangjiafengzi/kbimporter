from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _extras() -> dict[str, list[str]]:
    with open(ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["optional-dependencies"]


def test_all_extras_cover_every_component():
    all_extras = _extras()["all"]
    expected = [
        "pymilvus", "pypinyin", "PyMuPDF", "EbookLib",
        "beautifulsoup4", "marker-pdf", "markitdown",
        "openai", "requests", "pdfplumber",
    ]
    for dep in expected:
        assert any(dep.lower() in d.lower() for d in all_extras), f"缺少 {dep}"


def test_cloud_extras_include_requests():
    assert any("requests" in d for d in _extras()["cloud"])
