from __future__ import annotations

from kbimporter.doctor import scan_environment
from kbimporter.setup import _set_config_value, run_setup, venv_status
from kbimporter.util import setup_logging


def test_doctor_scan_returns_structure(cfg, monkeypatch):
    def _fail(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr("socket.create_connection", _fail)
    monkeypatch.setattr("kbimporter.doctor._candidate_interpreters", lambda: [])
    info = scan_environment(cfg)
    assert "python" in info
    assert "packages" in info
    assert "exes" in info
    assert "env_keys" in info
    assert "PADDLE_OCR_API_KEY" in info["env_keys"]
    assert info["milvus_reachable"] is False
    assert info["state_db"]["path"] == str(cfg.state_db)
    assert "interpreters" in info


def test_setup_non_interactive_prints_guidance(cfg, capsys):
    rc = run_setup(cfg, logger=setup_logging())
    assert rc == 0
    out = capsys.readouterr().out
    assert "非交互模式" in out
    assert "kb doctor" in out


def test_set_config_value_edits_section(tmp_path):
    p = tmp_path / "kb_config.toml"
    p.write_text(
        "[cloud_ocr]\nenabled = false\nprovider = \"openai\"\n[paths]\nx = 1\n",
        encoding="utf-8",
    )
    assert _set_config_value(p, "cloud_ocr.enabled", True) is True
    assert _set_config_value(p, "cloud_ocr.provider", "paddle") is True
    content = p.read_text(encoding="utf-8")
    assert "[cloud_ocr]\nenabled = true\nprovider = \"paddle\"" in content
    assert "[paths]\nx = 1" in content


def test_venv_status_detects_existing(tmp_path):
    missing = tmp_path / "no-venv"
    assert venv_status(missing) == "missing"
    bare = tmp_path / "bare-venv"
    (bare / "Scripts").mkdir(parents=True)
    assert venv_status(bare) == "exists_without_kb"
    with_kb = tmp_path / "kb-venv"
    (with_kb / "Scripts").mkdir(parents=True)
    (with_kb / "Scripts" / "kb.exe").write_bytes(b"")
    assert venv_status(with_kb) == "exists_with_kb"
