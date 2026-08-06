from __future__ import annotations

from pathlib import Path

from kbimporter.config_edit import set_toml_value


def test_set_existing_and_new_values(tmp_path: Path):
    p = tmp_path / "kb_config.toml"
    p.write_text(
        "[cloud_ocr]\nenabled = false\nprovider = \"openai\"\n[paths]\nx = 1\n",
        encoding="utf-8",
    )
    assert set_toml_value(p, "cloud_ocr.enabled", True) is True
    assert set_toml_value(p, "cloud_ocr.provider", "paddle") is True
    content = p.read_text(encoding="utf-8")
    assert "[cloud_ocr]\nenabled = true\nprovider = \"paddle\"" in content
    assert "[paths]\nx = 1" in content


def test_set_list_value(tmp_path: Path):
    p = tmp_path / "kb_config.toml"
    p.write_text("[converter]\nengines = [\"marker\", \"mineru\", \"cloud\"]\n", encoding="utf-8")
    assert set_toml_value(p, "converter.engines", ["marker", "mineru"]) is True
    content = p.read_text(encoding="utf-8")
    assert "engines = [\"marker\", \"mineru\"]" in content


def test_set_creates_missing_section(tmp_path: Path):
    p = tmp_path / "kb_config.toml"
    p.write_text("[paths]\nx = 1\n", encoding="utf-8")
    assert set_toml_value(p, "cloud_ocr.enabled", True) is True
    content = p.read_text(encoding="utf-8")
    assert "[cloud_ocr]\nenabled = true" in content
