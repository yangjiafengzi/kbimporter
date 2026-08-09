from __future__ import annotations

from pathlib import Path

from kbimporter.config import Config, load_config


def _write_toml(path: Path, kb_root: str):
    path.write_text(
        f"""[paths]
kb_root = "{kb_root}"
[milvus]
host = "10.0.0.1"
port = "19531"
batch_size = 7
[converter]
max_per_batch = 5
after_convert = "delete"
[dedupe]
replace_existing_md = "never"
""",
        encoding="utf-8",
    )


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def test_load_config_derives_paths(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KB_ROOT", raising=False)
    _write_toml(tmp_path / "kb_config.toml", _posix(tmp_path / "kb"))
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.kb_root == tmp_path / "kb"
    assert cfg.library_dir == tmp_path / "kb" / "zotero文献库" / "library"
    assert cfg.project_root == tmp_path / "kb" / "项目文献"
    assert cfg.state_db == tmp_path / "kb" / ".kb" / "state.db"
    assert cfg.trash_dir == tmp_path / "kb" / ".kb" / "trash"
    assert cfg.milvus.host == "10.0.0.1"
    assert cfg.milvus.batch_size == 7
    assert cfg.max_per_batch == 5


def test_load_config_discovers_via_kb_config_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KB_ROOT", raising=False)
    p = tmp_path / "kb_config.toml"
    _write_toml(p, _posix(tmp_path / "kb"))
    monkeypatch.setenv("KB_CONFIG", str(p))
    cfg = load_config()
    assert cfg.kb_root == tmp_path / "kb"
    assert cfg.config_path == p


def test_load_config_overrides_sections(tmp_path: Path):
    _write_toml(tmp_path / "kb_config.toml", _posix(tmp_path / "kb"))
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.max_per_batch == 5
    assert cfg.after_convert == "delete"
    assert cfg.replace_existing_md == "never"


def test_env_overrides(tmp_path: Path, monkeypatch):
    _write_toml(tmp_path / "kb_config.toml", _posix(tmp_path / "kb"))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "other"))
    monkeypatch.setenv("MILVUS_HOST", "127.0.0.9")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    cfg = load_config(tmp_path / "kb_config.toml")
    assert cfg.kb_root == tmp_path / "other"
    assert cfg.milvus.host == "127.0.0.9"
    assert cfg.dashscope_api_key == "sk-test"
    assert cfg.llm_api_key == "sk-test"


def test_secrets_section_is_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    p = tmp_path / "kb_config.toml"
    p.write_text(
        f"""[paths]
kb_root = "{_posix(tmp_path / 'kb')}"
[secrets]
dashscope_api_key = "cfg-key"
llm_api_key = "cfg-key-2"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.dashscope_api_key == ""
    assert cfg.llm_api_key == ""


def test_missing_kb_root_raises(tmp_path: Path):
    cfg = Config()
    cfg.derive()
    try:
        cfg.require_kb_root()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_state_db_path_override(tmp_path: Path):
    legacy = tmp_path / "0向量化" / "import_state.db"
    cfg = Config(kb_root=tmp_path / "kb", state_db_path=legacy)
    cfg.derive()
    assert cfg.state_db == legacy
    assert cfg.trash_dir == tmp_path / "kb" / ".kb" / "trash"


def test_cloud_ocr_paddle_default_and_parse(tmp_path: Path):
    cfg = Config()
    assert cfg.cloud_ocr.provider == "paddle"
    assert cfg.cloud_ocr.paddle.job_url == "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    cfg_file = tmp_path / "kb_config.toml"
    cfg_file.write_text(
        """[paths]
kb_root = "C:/tmp/kb"
[cloud_ocr]
provider = "paddle"
[cloud_ocr.paddle]
job_url = "https://example.com/api/v2/ocr/jobs"
model = "PaddleOCR-Test"
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.cloud_ocr.provider == "paddle"
    assert loaded.cloud_ocr.paddle.job_url == "https://example.com/api/v2/ocr/jobs"
    assert loaded.cloud_ocr.paddle.model == "PaddleOCR-Test"


def test_cloud_ocr_mineru_parse(tmp_path: Path):
    cfg = Config()
    assert cfg.cloud_ocr.mineru.api_key_env == "MINERU_API_KEY"
    assert cfg.cloud_ocr.mineru.model_version == "vlm"
    cfg_file = tmp_path / "kb_config.toml"
    cfg_file.write_text(
        """[paths]
kb_root = "C:/tmp/kb"
[cloud_ocr]
provider = "paddle"
fallback_providers = ["mineru"]
[cloud_ocr.mineru]
api_key_env = "MY_MINERU_KEY"
model_version = "pipeline"
is_ocr = false
max_pages_per_task = 50
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.cloud_ocr.fallback_providers == ["mineru"]
    assert loaded.cloud_ocr.mineru.api_key_env == "MY_MINERU_KEY"
    assert loaded.cloud_ocr.mineru.model_version == "pipeline"
    assert loaded.cloud_ocr.mineru.is_ocr is False
    assert loaded.cloud_ocr.mineru.max_pages_per_task == 50


def test_cloud_ocr_concurrency_timeout_parse(tmp_path: Path):
    cfg_file = tmp_path / "kb_config.toml"
    cfg_file.write_text(
        """[paths]
kb_root = "C:/tmp/kb"
[cloud_ocr]
max_files_workers = 3
[cloud_ocr.paddle]
max_workers = 4
stall_timeout = 600
[cloud_ocr.mineru]
max_workers = 3
stall_timeout = 1200
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.cloud_ocr.max_files_workers == 3
    assert loaded.cloud_ocr.paddle.max_workers == 4
    assert loaded.cloud_ocr.paddle.stall_timeout == 600
    assert loaded.cloud_ocr.mineru.max_workers == 3
    assert loaded.cloud_ocr.mineru.stall_timeout == 1200
