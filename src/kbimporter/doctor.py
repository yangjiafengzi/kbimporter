from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from kbimporter.config import Config


def _package_installed(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _env_exe(env: str, exe: str) -> str | None:
    for base in (Path.home() / "miniconda3" / "envs",
                 Path.home() / "anaconda3" / "envs"):
        p = (base / env / "Scripts" / exe) if env else (base.parent / "Scripts" / exe)
        if p.exists():
            return str(p)
    if not env:
        for base in (Path.home() / "miniconda3", Path.home() / "anaconda3"):
            p = base / "Scripts" / exe
            if p.exists():
                return str(p)
    return None


def _candidate_interpreters() -> list[str]:
    """收集本机常见 Python 解释器（当前 venv + base + conda envs + PATH）。"""
    found: list[str] = []

    def add(p: Path):
        if p.exists() and str(p) not in found:
            found.append(str(p))

    add(Path(sys.executable))
    home = Path.home()
    for base_name in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"):
        base = home / base_name
        add(base / "python.exe")
        add(base / "bin" / "python")
        envs = base / "envs"
        if envs.is_dir():
            for env in sorted(envs.iterdir()):
                if env.is_dir():
                    add(env / "Scripts" / "python.exe")
                    add(env / "bin" / "python")
    for p in (shutil.which("python"), shutil.which("python3")):
        if p:
            add(Path(p))
    return found


def _probe_interpreter(py: str, packages: list[str]) -> dict | None:
    code = (
        "import importlib.util as u, json; "
        f"print(json.dumps({{n: bool(u.find_spec(n)) for n in {packages!r}}}))"
    )
    try:
        out = subprocess.run(
            [py, "-c", code],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        if out.returncode == 0:
            return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    return None


def scan_environment(cfg: Config) -> dict:
    """只读扫描：Python、依赖包、外部引擎、密钥、Milvus 可达性。"""
    result: dict = {}
    result["python"] = sys.version.split()[0]
    result["python_exe"] = sys.executable
    result["pip"] = bool(_which("pip", "pip3"))

    result["packages"] = {
        "pypinyin": _package_installed("pypinyin"),
        "pymilvus": _package_installed("pymilvus"),
        "pymupdf": _package_installed("fitz"),
        "ebooklib": _package_installed("ebooklib"),
        "beautifulsoup4": _package_installed("bs4"),
        "pdfplumber": _package_installed("pdfplumber"),
        "openai": _package_installed("openai"),
    }
    result["exes"] = {
        "marker": _which("marker") or _env_exe("ocr_env", "marker.exe"),
        "marker_single": _which("marker_single") or _env_exe("ocr_env", "marker_single.exe"),
        "markitdown": _which("markitdown") or _env_exe("", "markitdown.exe"),
        "mineru": _which("mineru") or _env_exe("mineru_env", "mineru.exe"),
    }
    result["env_keys"] = {
        "DASHSCOPE_API_KEY": bool(os.environ.get("DASHSCOPE_API_KEY")),
        "LLM_API_KEY": bool(os.environ.get("LLM_API_KEY")),
        "BAIDU_OCR_API_KEY": bool(os.environ.get("BAIDU_OCR_API_KEY")),
        "BAIDU_OCR_SECRET_KEY": bool(os.environ.get("BAIDU_OCR_SECRET_KEY")),
        "PADDLE_OCR_API_KEY": bool(os.environ.get("PADDLE_OCR_API_KEY")),
    }
    try:
        with socket.create_connection(
            (cfg.milvus.host, int(cfg.milvus.port)), timeout=2
        ):
            result["milvus_reachable"] = True
    except Exception:
        result["milvus_reachable"] = False

    result["dirs"] = {
        "library": bool(cfg.library_dir and cfg.library_dir.exists()),
        "project": bool(cfg.project_root and cfg.project_root.exists()),
        "fieldwork": bool(cfg.fieldwork_root and cfg.fieldwork_root.exists()),
        "zotero_storage": bool(cfg.zotero_storage and cfg.zotero_storage.exists()),
    }
    result["state_db"] = {
        "path": str(cfg.state_db) if cfg.state_db else "",
        "exists": bool(cfg.state_db and cfg.state_db.exists()),
    }
    result["cloud_ocr_enabled"] = cfg.cloud_ocr.enabled
    result["cloud_ocr_provider"] = cfg.cloud_ocr.provider
    result["interpreters"] = []
    for py in _candidate_interpreters():
        if Path(py) == Path(sys.executable):
            continue
        pkgs = _probe_interpreter(py, list(result["packages"].keys()))
        if pkgs is not None:
            result["interpreters"].append({"path": py, "packages": pkgs})
    return result


def _ok(flag: bool) -> str:
    return "✓" if flag else "✗"


def print_report(info: dict, log: logging.Logger):
    log.info("=" * 60)
    log.info("kb doctor 环境体检")
    log.info("=" * 60)
    log.info(f"Python: {info['python']} ({info['python_exe']})")
    log.info(f"pip: {'✓ 可用' if info['pip'] else '✗ 未找到'}")
    log.info("-" * 60)
    log.info("Python 依赖:")
    for name, ok in info["packages"].items():
        log.info(f"  {_ok(ok)} {name}")
    log.info("外部引擎:")
    for name, path in info["exes"].items():
        log.info(f"  {_ok(bool(path))} {name}: {path or '未找到'}")
    log.info("环境变量（仅云端 OCR / LLM OCR 需要；向量化由 Milvus 服务端完成）:")
    for name, ok in info["env_keys"].items():
        log.info(f"  {_ok(ok)} {name}")
    log.info(f"Milvus: {_ok(info['milvus_reachable'])} "
             f"{info['milvus_reachable'] and '可达' or '不可达（未启动/未安装）'}")
    log.info("知识库目录:")
    for name, ok in info["dirs"].items():
        log.info(f"  {_ok(ok)} {name}")
    log.info(f"状态库: {info['state_db']['path']} "
             f"({'已存在' if info['state_db']['exists'] else '尚未创建'})")
    log.info(f"云端 OCR: {'已启用（注意费用）' if info['cloud_ocr_enabled'] else '未启用'}")
    if info.get("interpreters"):
        log.info("其他 Python 环境（可复用依赖）:")
        for env in info["interpreters"]:
            found = [k for k, v in env["packages"].items() if v]
            log.info(f"  {env['path']}: {', '.join(found) if found else '（无目标依赖）'}")
    log.info("-" * 60)
    log.info("下一步建议:")
    missing_exes = [k for k, v in info["exes"].items() if not v]
    if missing_exes:
        log.info("  1. 运行 `kb setup` 按引导安装缺失依赖/引擎")
    elif not info["milvus_reachable"]:
        log.info("  1. 启动 Milvus（如 Docker Desktop 中的 milvus 容器）")
    if info.get("cloud_ocr_enabled"):
        needed = {
            "paddle": ["PADDLE_OCR_API_KEY"],
            "baidu": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"],
            "openai": ["DASHSCOPE_API_KEY"],
        }.get(info.get("cloud_ocr_provider", ""), [])
        missing = [k for k in needed if not info["env_keys"].get(k)]
        if missing:
            log.info(f"  2. 设置 {', '.join(missing)}（云端 OCR provider={info.get('cloud_ocr_provider')} 需要）")
    else:
        log.info("  2. 云端 OCR 未启用，无需 API Key（向量化由 Milvus 服务端完成）")
    log.info("  3. 运行 `kb status` 查看知识库，`kb import --dry-run` 预演导入")


def run_doctor(cfg: Config, logger: logging.Logger | None = None) -> dict:
    log = logger or logging.getLogger("kbimporter")
    info = scan_environment(cfg)
    print_report(info, log)
    return info
