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


def test_cli_init_fills_custom_zotero_storage(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from kbimporter import cli
    monkeypatch.setattr(
        cli, "detect_zotero_storage", lambda: (Path(r"D:\Zotero\storage"), True)
    )
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive"])
    assert rc == 0
    content = (tmp_path / "kb_config.toml").read_text(encoding="utf-8")
    assert 'zotero_storage = "D:\\\\Zotero\\\\storage"' in content
    from kbimporter.config import load_config
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.zotero_storage == Path(r"D:\Zotero\storage")


def test_cli_global_config_before_subcommand(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive"])
    assert rc == 0
    rc = main(["--config", "kb_config.toml", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "知识库根目录" in out
    assert str(tmp_path / "kb") in out


def test_cli_help_command(capsys):
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage: kb" in out
    assert "import" in out
    assert "release" in out


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


def test_env_in_shell_files(tmp_path: Path, monkeypatch):
    from kbimporter import cli
    rc = tmp_path / ".zshrc"
    rc.write_text(
        'export PADDLE_OCR_API_KEY="abc"\n'
        "set -gx MINERU_API_KEY token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_shell_rc_files", lambda: [rc])
    assert cli._env_in_shell_files("PADDLE_OCR_API_KEY") is True
    assert cli._env_in_shell_files("MINERU_API_KEY") is True
    assert cli._env_in_shell_files("DASHSCOPE_API_KEY") is False


def test_cli_shell_init_apply_writes_profile(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive"])
    assert rc == 0
    from kbimporter import cli
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    rc = main(["shell-init", "--config", "kb_config.toml", "--apply"])
    assert rc == 0
    profile = cli._shell_profile_path()
    assert profile.exists()
    text = profile.read_text(encoding="utf-8")
    assert "kbimporter shell-init" in text
    assert "KB_CONFIG" in text
    # 幂等：再次执行不追加
    rc = main(["shell-init", "--config", "kb_config.toml", "--apply"])
    assert rc == 0
    assert profile.read_text(encoding="utf-8") == text


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


def test_cli_dedupe_executes_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KB_ROOT", raising=False)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive"])
    assert rc == 0
    proj = tmp_path / "kb" / "项目文献"
    pdf = proj / "a.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"x")
    (proj / "a.md").write_text("md", encoding="utf-8")

    rc = main(["dedupe", "--config", "kb_config.toml", "--scope", "project"])
    assert rc == 0
    assert not pdf.exists()


def test_cli_release_releases_collections(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KB_ROOT", raising=False)
    rc = main(["init", "--root", str(tmp_path / "kb"), "--output", "kb_config.toml",
               "--non-interactive"])
    assert rc == 0
    from kbimporter import models

    class _FakeClient:
        def __init__(self):
            self.released: list[str] = []

        def list_collections(self, **kwargs):
            return ["a", "b"]

        def release_collection(self, collection_name, **kwargs):
            self.released.append(collection_name)

    fake = _FakeClient()
    monkeypatch.setattr(models, "get_client", lambda cfg: fake)

    rc = main(["release", "--config", "kb_config.toml"])
    assert rc == 0
    assert fake.released == ["a", "b"]

    rc = main(["release", "academic_library", "--config", "kb_config.toml"])
    assert rc == 0
    assert fake.released == ["a", "b", "academic_library"]
