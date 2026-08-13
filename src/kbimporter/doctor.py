from __future__ import annotations

import importlib.util
import importlib.metadata
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from kbimporter.config import Config
from kbimporter.zotero_sync import detect_zotero_storage


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


def _pip_version() -> str:
    """返回当前解释器的 pip 版本；检测不到时返回空字符串。"""
    try:
        return importlib.metadata.version("pip")
    except Exception:
        pass
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if out.returncode == 0 and out.stdout.split():
            return out.stdout.split()[1]
    except Exception:
        pass
    return ""


def python_ocr_warning(python_version: tuple[int, ...] | None = None) -> str | None:
    """Python 3.13+ 时提示本地 OCR 重型依赖可能缺少预编译 wheel。"""
    version = python_version or sys.version_info[:3]
    if version >= (3, 13):
        return (
            f"当前 Python {'.'.join(map(str, version))}：本地 OCR（[ocr] 中的 "
            "marker-pdf / PyTorch / opencv / onnxruntime）在 3.13+ 上可能缺少"
            "预编译 wheel，建议使用 Python 3.11/3.12 安装本地 OCR，"
            "或改用云端 OCR。"
        )
    return None


def _env_exe(env: str, exe: str) -> str | None:
    names = [exe]
    if exe.lower().endswith(".exe"):
        names.append(exe[:-4])
    else:
        names.append(exe + ".exe")
    for base in (Path.home() / "miniconda3", Path.home() / "anaconda3",
                 Path.home() / "miniforge3", Path.home() / "mambaforge"):
        env_dir = (base / "envs" / env) if env else base
        for sub in ("Scripts", "bin"):
            for name in names:
                p = env_dir / sub / name
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
    result["pip"] = _pip_version()
    result["python_ocr_warning"] = python_ocr_warning()

    result["packages"] = {
        "pypinyin": _package_installed("pypinyin"),
        "pymilvus": _package_installed("pymilvus"),
        "pymupdf": _package_installed("pymupdf") or _package_installed("fitz"),
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
        "MINERU_API_KEY": bool(os.environ.get("MINERU_API_KEY")),
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
    detected_storage, zotero_custom = detect_zotero_storage()
    result["zotero_detected"] = str(detected_storage) if detected_storage else ""
    result["zotero_custom"] = zotero_custom
    result["zotero_configured"] = str(cfg.zotero_storage) if cfg.zotero_storage else ""
    result["zotero_mismatch"] = bool(
        zotero_custom
        and cfg.zotero_storage
        and Path(cfg.zotero_storage) != detected_storage
    )
    result["state_db"] = {
        "path": str(cfg.state_db) if cfg.state_db else "",
        "exists": bool(cfg.state_db and cfg.state_db.exists()),
    }
    result["milvus_embedding_verified"] = False
    result["cloud_ocr_enabled"] = cfg.cloud_ocr.enabled
    result["cloud_ocr_provider"] = cfg.cloud_ocr.provider
    result["cloud_ocr_fallback_providers"] = list(
        cfg.cloud_ocr.fallback_providers or []
    )
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
    pip_ver = info["pip"]
    log.info(f"pip: {'✓ ' + pip_ver if pip_ver else '✗ 未找到'}")
    if info.get("python_ocr_warning"):
        log.warning(f"⚠ {info['python_ocr_warning']}")
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
    if info["milvus_reachable"]:
        if info.get("milvus_embedding_verified"):
            log.info("Milvus: ✓ 可达，向量化链路已验证（--deep）")
        else:
            log.info("Milvus: ✓ 可达（向量化链路未验证，请运行 `kb doctor --deep`）")
            log.info("  注意: 可达 ≠ 可向量化；若服务端未配置 "
                     "MILVUSAI_DASHSCOPE_API_KEY（或 deploy/user.yaml 的 "
                     "credential），`kb import` 仍会失败。")
    else:
        log.info("Milvus: ✗ 不可达（未启动/未安装），请先启动 Milvus "
                 "（如 scripts/start-milvus.ps1 或 docker compose）。")
    log.info("知识库目录:")
    for name, ok in info["dirs"].items():
        log.info(f"  {_ok(ok)} {name}")
    if info.get("zotero_mismatch"):
        log.warning(f"  ⚠ 检测到 Zotero 自定义数据目录: {info['zotero_detected']}")
        log.warning(
            f"    当前 [paths].zotero_storage = {info['zotero_configured']}，"
            "二者不一致，`kb sync-zotero` 可能扫描不到文件。"
        )
        log.warning("    建议将配置改为检测到的路径，或重新运行 "
                    "`kb init --force` 自动填入。")
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
        provider_keys = {
            "paddle": ["PADDLE_OCR_API_KEY"],
            "mineru": ["MINERU_API_KEY"],
            "baidu": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"],
            "openai": ["DASHSCOPE_API_KEY"],
        }
        chain = [info.get("cloud_ocr_provider", "")] + list(
            info.get("cloud_ocr_fallback_providers") or []
        )
        needed: list[str] = []
        for p in chain:
            for k in provider_keys.get(p, []):
                if k not in needed:
                    needed.append(k)
        missing = [k for k in needed if not info["env_keys"].get(k)]
        if missing:
            log.info(
                f"  2. 设置 {', '.join(missing)}（云端 OCR provider 链: "
                f"{' -> '.join(chain)} 需要）"
            )
    else:
        log.info("  2. 云端 OCR 未启用，无需 API Key（向量化由 Milvus 服务端完成）")
    log.info("  3. 运行 `kb status` 查看知识库，`kb import --dry-run` 预演导入")


def run_doctor(cfg: Config, logger: logging.Logger | None = None,
               deep: bool = False) -> dict:
    log = logger or logging.getLogger("kbimporter")
    info = scan_environment(cfg)
    if deep:
        info["milvus_embedding_verified"] = check_embedding_chain(cfg, log)
    print_report(info, log)
    return info


def _embedding_troubleshooting(cfg: Config) -> str:
    return (
        "排障建议：\n"
        "  1. Milvus 服务端已配置 MILVUSAI_DASHSCOPE_API_KEY（或 user.yaml 的 credential）\n"
        "  2. 自定义端点时 url 必须是 DashScope 原生 embeddings 地址：\n"
        "     https://<你的端点>/api/v1/services/embeddings/text-embedding/text-embedding\n"
        "     （不是 OpenAI 兼容的 compatible-mode 地址）\n"
        f"  3. embedding_model = {cfg.milvus.embedding_model} 需与 Milvus 服务端配置一致\n"
        f"  4. embedding_dim = {cfg.milvus.embedding_dim} 需与模型输出维度一致"
        "（text-embedding-v3 为 1024）\n"
        "  5. 出现 404 / 数量不匹配时优先检查第 1、2 项"
    )


def check_embedding_chain(cfg: Config, log: logging.Logger) -> bool:
    """端到端嵌入体检：临时建集合 -> insert -> flush/load/query -> 校验维度 -> 删除。"""
    probe = "_probe_kbimporter"
    log.info("=" * 60)
    log.info(f"kb doctor --deep：端到端嵌入体检（临时集合 {probe}，结束后自动删除）")
    log.info("=" * 60)
    try:
        from pymilvus import MilvusClient, DataType, Function, FunctionType
    except ImportError:
        log.info("✗ pymilvus 未安装，无法体检嵌入链路")
        return False

    client = None
    try:
        client = MilvusClient(
            uri=f"http://{cfg.milvus.host}:{cfg.milvus.port}", timeout=10
        )
        if client.has_collection(collection_name=probe):
            client.drop_collection(collection_name=probe)
        schema = MilvusClient.create_schema(auto_id=True, description="kb doctor 临时体检")
        schema.add_field(
            field_name="id", datatype=DataType.INT64, is_primary=True, auto_id=True
        )
        schema.add_field(
            field_name="text", datatype=DataType.VARCHAR, max_length=512
        )
        schema.add_field(
            field_name="vector", datatype=DataType.FLOAT_VECTOR,
            dim=cfg.milvus.embedding_dim,
        )
        fn = Function(
            name="text_dense_emb",
            input_field_names=["text"],
            output_field_names=["vector"],
            function_type=FunctionType.TEXTEMBEDDING,
            params={
                "provider": cfg.milvus.embedding_provider,
                "model_name": cfg.milvus.embedding_model,
            },
        )
        schema.add_function(fn)
        client.create_collection(collection_name=probe, schema=schema)
        params = client.prepare_index_params()
        params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="IP",
            params={
                "M": cfg.milvus.hnsw_m,
                "efConstruction": cfg.milvus.hnsw_ef_construction,
            },
        )
        client.create_index(collection_name=probe, index_params=params)
        client.insert(collection_name=probe, data=[{"text": "测试嵌入功能"}])
        client.flush(collection_name=probe)
        client.load_collection(collection_name=probe)
        rows = client.query(
            collection_name=probe,
            filter="id >= 0",
            output_fields=["text", "vector"],
            limit=1,
        )
        if not rows:
            log.info("✗ 插入后查询无结果")
            log.info(_embedding_troubleshooting(cfg))
            return False
        vec = rows[0].get("vector") or []
        if len(vec) != cfg.milvus.embedding_dim:
            log.info(
                f"✗ 维度不匹配：实际 {len(vec)}，配置 {cfg.milvus.embedding_dim}"
            )
            log.info(_embedding_troubleshooting(cfg))
            return False
        log.info(
            f"✓ 嵌入链路正常：provider={cfg.milvus.embedding_provider}, "
            f"model={cfg.milvus.embedding_model}, 维度={len(vec)}"
        )
        return True
    except Exception as e:
        log.info(f"✗ 嵌入链路失败: {e}")
        log.info(_embedding_troubleshooting(cfg))
        return False
    finally:
        if client is not None:
            try:
                if client.has_collection(collection_name=probe):
                    client.drop_collection(collection_name=probe)
                    log.info(f"✓ 已清理临时集合 {probe}")
            except Exception:
                log.info(f"⚠ 临时集合 {probe} 清理失败，请手动删除")
            try:
                client.close()
            except Exception:
                pass
