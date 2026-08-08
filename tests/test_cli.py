from __future__ import annotations

from pathlib import Path

from kbimporter.cli import main


def test_cli_init_creates_config(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KB_ROOT", raising=False)
    rc = main(["init", "--root", str(tmp_path / "知识库"), "--output", "kb_config.toml"])
    assert rc == 0
    cfg_file = tmp_path / "kb_config.toml"
    assert cfg_file.exists()
    content = cfg_file.read_text(encoding="utf-8")
    toml_root = str(tmp_path / "知识库").replace("\\", "/")
    assert f'kb_root = "{toml_root}"' in content
    assert (tmp_path / "知识库" / "项目文献").is_dir()
    from kbimporter.config import load_config
    cfg = load_config(cfg_file)
    assert cfg.kb_root == tmp_path / "知识库"


def test_cli_init_refuses_overwrite(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kb_config.toml").write_text("old", encoding="utf-8")
    rc = main(["init", "--root", "C:/tmp", "--output", "kb_config.toml"])
    assert rc == 1
    assert "已存在" in capsys.readouterr().out


def test_cli_help_command(capsys):
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: kb" in out
    assert "import" in out


def test_cli_help_subcommand(capsys):
    rc = main(["help", "import"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out


def test_cli_ocr_mode_switch_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive", "--force"])
    assert rc == 0

    rc = main(["ocr", "mode", "cloud", "--provider", "paddle", "--config", "kb_config.toml"])
    assert rc == 0
    from kbimporter.config import load_config
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.cloud_ocr.enabled is True
    assert cfg.cloud_ocr.provider == "paddle"
    assert cfg.engines == ["cloud"]

    rc = main(["ocr", "status", "--config", "kb_config.toml"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cloud" in out

    rc = main(["ocr", "disable", "--config", "kb_config.toml"])
    assert rc == 0
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.cloud_ocr.enabled is False
    assert cfg.engines == ["marker", "mineru"]


def test_cli_ocr_keys(capsys):
    rc = main(["ocr", "keys"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "paddle" in out and "PADDLE_OCR_API_KEY" in out
    assert "mineru" in out and "MINERU_API_KEY" in out


def test_cli_ocr_enable_mineru_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive", "--force"])
    assert rc == 0

    rc = main(["ocr", "enable", "--provider", "paddle", "--fallback", "mineru",
               "--config", "kb_config.toml"])
    assert rc == 0
    from kbimporter.config import load_config
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.cloud_ocr.enabled is True
    assert cfg.cloud_ocr.provider == "paddle"
    assert cfg.cloud_ocr.fallback_providers == ["mineru"]

    rc = main(["ocr", "mode", "cloud", "--provider", "mineru", "--fallback", "none",
               "--config", "kb_config.toml"])
    assert rc == 0
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.cloud_ocr.provider == "mineru"
    assert cfg.cloud_ocr.fallback_providers == []


def test_cli_ocr_mode_hybrid_priority(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive", "--force"])
    assert rc == 0

    rc = main(["ocr", "mode", "hybrid", "cloud", "--provider", "paddle",
               "--config", "kb_config.toml"])
    assert rc == 0
    from kbimporter.config import load_config
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.cloud_ocr.enabled is True
    assert cfg.cloud_ocr.provider == "paddle"
    assert cfg.engines == ["cloud", "marker", "mineru"]

    rc = main(["ocr", "status", "--config", "kb_config.toml"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hybrid（云端优先）" in out

    rc = main(["ocr", "mode", "hybrid", "local", "--provider", "paddle",
               "--config", "kb_config.toml"])
    assert rc == 0
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.engines == ["marker", "mineru", "cloud"]

    rc = main(["ocr", "status", "--config", "kb_config.toml"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hybrid（本地优先）" in out
