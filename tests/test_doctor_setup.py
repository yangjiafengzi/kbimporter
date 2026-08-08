from __future__ import annotations

import sys
import types

from kbimporter import doctor
from kbimporter.doctor import scan_environment
from kbimporter.setup import (
    CLOUD_OCR_EXTRAS,
    CORE_EXTRAS,
    LOCAL_OCR_EXTRAS,
    _set_config_value,
    run_setup,
    scheme_extras,
    venv_status,
)
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
    assert "MINERU_API_KEY" in info["env_keys"]
    assert info["milvus_reachable"] is False
    assert info["state_db"]["path"] == str(cfg.state_db)
    assert "interpreters" in info
    assert isinstance(info["pip"], str)


def test_setup_non_interactive_prints_guidance(cfg, capsys):
    rc = run_setup(cfg, logger=setup_logging())
    assert rc == 0
    out = capsys.readouterr().out
    assert "非交互模式" in out
    assert "kb doctor" in out
    assert "[import,sync,dedupe,ocr]" in out
    assert "[import,sync,dedupe,cloud]" in out


def test_scheme_extras_mapping():
    assert scheme_extras("1") == [*CORE_EXTRAS, *LOCAL_OCR_EXTRAS]
    assert scheme_extras("2") == [*CORE_EXTRAS, *CLOUD_OCR_EXTRAS]
    assert scheme_extras("3") == [*CORE_EXTRAS, *LOCAL_OCR_EXTRAS, *CLOUD_OCR_EXTRAS]


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


def test_env_exe_detects_scripts_and_bin(tmp_path, monkeypatch):
    from kbimporter import doctor
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: tmp_path))
    win = tmp_path / "miniconda3" / "envs" / "ocr_env" / "Scripts" / "marker.exe"
    win.parent.mkdir(parents=True)
    win.write_bytes(b"")
    posix = tmp_path / "miniconda3" / "envs" / "mineru_env" / "bin" / "mineru"
    posix.parent.mkdir(parents=True)
    posix.write_bytes(b"")
    assert doctor._env_exe("ocr_env", "marker.exe") == str(win)
    assert doctor._env_exe("mineru_env", "mineru") == str(posix)


def test_check_embedding_chain_ok_and_cleans_probe(cfg, monkeypatch):
    class _DataType:
        INT64 = "INT64"
        VARCHAR = "VARCHAR"
        FLOAT_VECTOR = "FLOAT_VECTOR"

    class _FunctionType:
        TEXTEMBEDDING = "TEXTEMBEDDING"

    class _Schema:
        def __init__(self, **kwargs):
            self.fields = []
            self.functions = []

        def add_field(self, field_name, datatype, **kwargs):
            self.fields.append(field_name)

        def add_function(self, fn):
            self.functions.append(fn)

    class _IndexParams:
        def add_index(self, field_name, index_type="", **kwargs):
            pass

    class _FakeClient:
        instances = []

        def __init__(self, uri="", **kwargs):
            self.uri = uri
            self.dropped = []
            self.rows = [{"text": "测试嵌入功能", "vector": [0.1] * cfg.milvus.embedding_dim}]
            _FakeClient.instances.append(self)

        @staticmethod
        def create_schema(**kwargs):
            return _Schema(**kwargs)

        def prepare_index_params(self):
            return _IndexParams()

        def has_collection(self, collection_name, **kwargs):
            return collection_name in self.dropped or collection_name == "_probe_kbimporter"

        def create_collection(self, collection_name, schema=None, **kwargs):
            pass

        def create_index(self, collection_name, index_params=None, **kwargs):
            pass

        def drop_collection(self, collection_name, **kwargs):
            self.dropped.append(collection_name)

        def insert(self, collection_name, data, **kwargs):
            pass

        def flush(self, collection_name, **kwargs):
            pass

        def load_collection(self, collection_name, **kwargs):
            pass

        def query(self, collection_name, filter="", output_fields=None, limit=None, **kwargs):
            return self.rows

        def close(self):
            pass

    fake = types.ModuleType("pymilvus")
    fake.MilvusClient = _FakeClient
    fake.DataType = _DataType
    fake.FunctionType = _FunctionType
    fake.Function = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "pymilvus", fake)

    ok = doctor.check_embedding_chain(cfg, doctor.logging.getLogger("t"))
    assert ok is True
    assert "_probe_kbimporter" in _FakeClient.instances[-1].dropped
