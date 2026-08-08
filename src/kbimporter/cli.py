from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from kbimporter import __version__
from kbimporter.config import DEFAULT_CONFIG_FILE, Config, load_config
from kbimporter.util import setup_logging


DEFAULT_CONFIG_TEMPLATE = """\
# 知识库导入程序配置文件（由 `kb init` 生成）
# 复制或直接修改本文件；所有密钥只从环境变量读取。

[paths]
kb_root = "{kb_root}"
library_dir = ""
project_root = ""
fieldwork_root = ""
zotero_storage = ""
state_dir = ""
state_db = ""
trash_dir = ""
scan_dir = ""
ocr_work_dir = ""
ocr_log_file = ""

[milvus]
host = "localhost"
port = "19530"
embedding_provider = "dashscope"
embedding_model = "text-embedding-v3"
embedding_dim = 1024
hnsw_m = 16
hnsw_ef_construction = 256
batch_size = 30

[chunk]
coarse_size = 8192
coarse_overlap = 1024
fine_size = 1024
fine_overlap = 256
separators = ["\\n\\n", "\\n", "。", "！", "？", "；", "，", " "]

[converter]
marker_cmd = "marker"
marker_single_cmd = "marker_single"
markitdown_cmd = "markitdown"
marker_workers = 1
max_per_batch = 20
timeout_per_pdf = 900
timeout_retry_pdf = 1800
engines = ["marker", "mineru", "cloud"]
mineru_cmd = "mineru"
mineru_backend = "hybrid-engine"
mineru_method = "ocr"
mineru_model_source = "auto"
enable_llm = false
llm_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
llm_model = "qwen3.5-plus"
after_convert = "trash"
skip_existing_md = true
skip_dirs = ["_convert_work", "ocr", "__pycache__", ".git", "0向量化", ".kb"]
skip_exts = [".pdf", ".md", ".json", ".py", ".log"]
markitdown_exts = [".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".epub", ".html", ".htm", ".csv", ".xml", ".txt", ".rtf", ".odt"]

[sync]
target_extensions = [".pdf", ".epub"]

[dedupe]
supported_extensions = [".pdf", ".epub"]
replace_existing_md = "ocr_only"

[cloud_ocr]
# 风险提示：云端 OCR 会调用付费 API，并把文档图片发送到第三方服务。
# 必须显式 enabled = true 才会启用。
enabled = false
provider = "paddle"
# 主 provider 失败后依次尝试的备选（需要对应密钥）
fallback_providers = ["mineru"]
state_dir = ""

[cloud_ocr.paddle]
job_url = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
model = "PaddleOCR-VL-1.6"
api_key_env = "PADDLE_OCR_API_KEY"
timeout = 120
max_retries = 3
poll_interval = 5
max_poll_seconds = 7200
use_doc_orientation_classify = false
use_doc_unwarping = false
use_chart_recognition = false

[cloud_ocr.mineru]
upload_url = "https://mineru.net/api/v4/file-urls/batch"
result_url = "https://mineru.net/api/v4/extract-results/batch"
api_key_env = "MINERU_API_KEY"
model_version = "vlm"
is_ocr = true
enable_formula = true
enable_table = true
language = "ch"
timeout = 120
max_retries = 3
poll_interval = 5
max_poll_seconds = 7200
max_pages_per_task = 200

[cloud_ocr.openai]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3-vl-plus"
api_key_env = "DASHSCOPE_API_KEY"
page_batch_size = 1
max_workers = 4
scale_factor = 3.0
timeout = 120
max_retries = 3
prompt = "你是一位专业的 OCR 识别专家。请识别图片中的全部文字，严格保持段落和排版结构，输出 Markdown 文本；忽略页眉、页脚、页码；模糊之处标注[?]；不要输出任何解释。"

[cloud_ocr.baidu]
token_url = "https://aip.baidubce.com/oauth/2.0/token"
accurate_url = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
api_key_env = "BAIDU_OCR_API_KEY"
secret_key_env = "BAIDU_OCR_SECRET_KEY"
language_type = "CHN_ENG"
detect_direction = true
paragraph = true
scale_factor = 2.0
max_workers = 4
timeout = 60
max_retries = 3
"""

def _config(args) -> "Config":
    return load_config(getattr(args, "config", None))


def cmd_init(args):
    out = Path(args.output).resolve() if args.output else Path(DEFAULT_CONFIG_FILE).resolve()
    if out.exists() and not args.force:
        print(f"配置文件已存在: {out}（使用 --force 覆盖）")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    interactive = args.interactive or (sys.stdin.isatty() and not args.non_interactive)
    kb_root = args.root or (input("知识库根目录（留空则仅生成配置）: ").strip() if interactive else "")
    if kb_root:
        root = Path(kb_root)
        for sub in ("zotero文献库/library", "项目文献", "田野调查笔记", ".kb"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        print(f"已创建/确认知识库目录结构: {root}")
    gpu = None
    if interactive:
        from kbimporter.setup import _ask_gpu
        gpu = _ask_gpu()
    toml_root = kb_root.replace("\\", "/") if kb_root else ""
    out.write_text(DEFAULT_CONFIG_TEMPLATE.format(kb_root=toml_root), encoding="utf-8")
    print(f"已生成配置模板: {out}")
    print(f"提示: 全局使用请设置环境变量 KB_CONFIG={out}，之后可在任意目录运行 kb 命令")
    print("请编辑 [paths].kb_root 等路径，并通过环境变量提供 API 密钥。")
    if gpu is not None:
        import logging
        from kbimporter.setup import print_engine_guidance
        print_engine_guidance(gpu, load_config(out), logging.getLogger("kbimporter"))
    print("下一步: kb doctor 体检环境, kb status 查看知识库, kb import --dry-run 预演导入")
    return 0


def cmd_status(args):
    cfg = _config(args)
    log = setup_logging()
    if cfg.config_path is None:
        print("提示: 未找到配置文件（查找顺序: --config > KB_CONFIG > 当前目录 > "
              "项目根 > %APPDATA%\\kbimporter）")
    root = cfg.kb_root
    print(f"知识库根目录: {root or '(未设置 KB_ROOT / [paths].kb_root)'}")
    for label, path in (
        ("Zotero 文献库", cfg.library_dir),
        ("项目文献", cfg.project_root),
        ("田野调查笔记", cfg.fieldwork_root),
        ("Zotero storage", cfg.zotero_storage),
    ):
        if path:
            exists = "存在" if path.exists() else "不存在"
            print(f"  {label}: {path} ({exists})")
    if root and root.exists():
        from kbimporter.scanner import scan_all_files
        files = scan_all_files(cfg)
        from collections import Counter
        counts = Counter()
        for fp in files:
            try:
                from kbimporter.scanner import classify_file
                info = classify_file(fp, cfg)
                counts[info["kb_type"] if info else "unclassified"] += 1
            except Exception:
                counts["unclassified"] += 1
        print(f"Markdown 文件: 共 {len(files)} 个")
        for k, v in sorted(counts.items()):
            print(f"  {k}: {v}")
    if cfg.state_db and cfg.state_db.exists():
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{cfg.state_db.as_posix()}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT COUNT(*), SUM(chunk_count) FROM file_state WHERE status='done'"
            ).fetchone()
            conn.close()
            print(f"增量状态: 已处理 {row[0] or 0} 个文件, 共 {row[1] or 0} 个切片")
        except sqlite3.Error as e:
            print(f"增量状态: 读取失败 ({e})")
    else:
        print(f"增量状态: 尚无状态库（将在 {cfg.state_db} 创建；"
              f"如需复用旧版 import_state.db，请把 [paths].state_db 直接指向该文件）")
    return 0


def cmd_import(args):
    cfg = _config(args)
    # 导入不写 OCR 日志文件，避免覆盖知识库中的历史日志
    log = setup_logging()
    from kbimporter.importer import run_import
    if args.dry_run:
        log.info("===== dry-run：只读预演，不写状态库、不调用 Milvus =====")
    else:
        log.info("===== 开始增量导入（需 Milvus 运行；embedding 由 Milvus 服务端完成）=====")
    run_import(cfg, dry_run=args.dry_run, logger=log)
    return 0


def cmd_sync(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.zotero_sync import sync_zotero
    stats = sync_zotero(cfg, dry_run=args.dry_run, logger=log)
    if stats.get("error"):
        return 1
    return 0


def cmd_convert(args):
    cfg = _config(args)
    # dry-run 不写日志文件，避免任何对知识库的写入
    log = setup_logging(cfg.ocr_log_file if not args.dry_run else None)
    from kbimporter.convert import run_convert
    scan_dir = Path(args.scan_dir) if args.scan_dir else None
    engine = None if args.engine == "auto" else args.engine
    run_convert(cfg, dry_run=args.dry_run, logger=log, scan_dir=scan_dir, engine=engine)
    return 0


def cmd_doctor(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.doctor import run_doctor
    run_doctor(cfg, logger=log, deep=args.deep)
    return 0


def cmd_help(args):
    parser = build_parser()
    if args.command:
        try:
            parser.parse_args([args.command, "--help"])
        except SystemExit:
            return 0
    parser.print_help()
    return 0


def _project_config_path(cfg) -> Path | None:
    from kbimporter.config import discover_config_path
    return discover_config_path(cfg.config_path)


def _key_envs(provider: str) -> list[str]:
    return {
        "paddle": ["PADDLE_OCR_API_KEY"],
        "mineru": ["MINERU_API_KEY"],
        "baidu": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"],
        "openai": ["DASHSCOPE_API_KEY"],
    }.get(provider, [])


def _apply_ocr_mode(path: Path, mode: str, provider: str,
                    fallback_providers: list[str] | None = None,
                    priority: str | None = None) -> str:
    from kbimporter.config_edit import set_toml_value
    if mode == "local":
        set_toml_value(path, "converter.engines", ["marker", "mineru"])
        set_toml_value(path, "cloud_ocr.enabled", False)
        return "本地模式：marker -> mineru（云端 OCR 关闭）"
    fallback_providers = fallback_providers or []
    set_toml_value(path, "cloud_ocr.fallback_providers", fallback_providers)
    fallback_text = (
        f"，备选 {' -> '.join(fallback_providers)}"
        if fallback_providers else ""
    )
    if mode == "cloud":
        set_toml_value(path, "converter.engines", ["cloud"])
        set_toml_value(path, "cloud_ocr.enabled", True)
        set_toml_value(path, "cloud_ocr.provider", provider)
        return f"云端模式：全部 PDF 走云端 OCR（provider={provider}{fallback_text}）"
    priority = priority or "local"
    engines = (
        ["cloud", "marker", "mineru"]
        if priority == "cloud"
        else ["marker", "mineru", "cloud"]
    )
    set_toml_value(path, "converter.engines", engines)
    set_toml_value(path, "cloud_ocr.enabled", True)
    set_toml_value(path, "cloud_ocr.provider", provider)
    label = "云端优先" if priority == "cloud" else "本地优先"
    return (
        f"混合模式（{label}）：{' -> '.join(engines)}"
        f"（provider={provider}{fallback_text}）"
    )


def _resolve_fallback(provider: str, fallback_arg: str | None) -> list[str]:
    if fallback_arg == "none":
        return []
    if fallback_arg:
        return [fallback_arg]
    return [] if provider == "mineru" else ["mineru"]


def _cloud_key_envs(cfg) -> list[str]:
    """当前云端 provider 链所需的全部环境变量。"""
    providers = [cfg.cloud_ocr.provider] + list(cfg.cloud_ocr.fallback_providers or [])
    envs: list[str] = []
    for p in providers:
        for k in _key_envs(p):
            if k not in envs:
                envs.append(k)
    return envs


def cmd_ocr_status(args):
    cfg = _config(args)
    cloud = cfg.cloud_ocr
    if not cloud.enabled:
        mode = "local"
    elif cfg.engines == ["cloud"]:
        mode = "cloud"
    elif cfg.engines and cfg.engines[0] == "cloud" and len(cfg.engines) > 1:
        mode = "hybrid（云端优先）"
    elif cfg.engines and "cloud" in cfg.engines:
        mode = "hybrid（本地优先）"
    else:
        mode = "hybrid（本地优先）"
    print(f"OCR 模式: {mode}")
    print("引擎链: " + " -> ".join(cfg.engines))
    cloud_chain = " -> ".join(
        [cloud.provider] + list(cloud.fallback_providers or [])
    )
    print(f"云端 OCR: {'已启用' if cloud.enabled else '未启用'}"
          + (f"（provider链: {cloud_chain}）" if cloud.enabled else ""))
    if cloud.enabled:
        envs = _cloud_key_envs(cfg)
        missing = [k for k in envs if not os.environ.get(k)]
        print("所需密钥: " + ("✓ 已设置" if not missing else "✗ 未设置: " + ", ".join(missing)))
    print(f"配置文件: {cfg.config_path or '未找到'}")
    return 0


def cmd_ocr_mode(args):
    cfg = _config(args)
    path = _project_config_path(cfg)
    if not path:
        print("未找到配置文件，请先运行 `kb init` 生成 kb_config.toml")
        return 1
    provider = args.provider or "paddle"
    fallback = _resolve_fallback(provider, getattr(args, "fallback", None))
    summary = _apply_ocr_mode(
        path, args.mode, provider, fallback,
        priority=getattr(args, "priority", None),
    )
    print(f"已切换为{summary}")
    missing = list(dict.fromkeys(
        k for k in _key_envs(provider) + _key_envs(fallback[0] if fallback else "")
        if not os.environ.get(k)
    ))
    if missing:
        print(f"还需设置环境变量: {', '.join(missing)}")
    print("预演: kb convert --dry-run；执行: kb convert")
    return 0


def cmd_ocr_enable(args):
    cfg = _config(args)
    path = _project_config_path(cfg)
    if not path:
        print("未找到配置文件，请先运行 `kb init` 生成 kb_config.toml")
        return 1
    provider = args.provider or "paddle"
    fallback = _resolve_fallback(provider, getattr(args, "fallback", None))
    summary = _apply_ocr_mode(path, "hybrid", provider, fallback)
    print(f"已启用云端 OCR：{summary}")
    missing = list(dict.fromkeys(
        k for k in _key_envs(provider) + _key_envs(fallback[0] if fallback else "")
        if not os.environ.get(k)
    ))
    if missing:
        print(f"还需设置环境变量: {', '.join(missing)}")
    return 0


def cmd_ocr_disable(args):
    cfg = _config(args)
    path = _project_config_path(cfg)
    if not path:
        print("未找到配置文件，请先运行 `kb init` 生成 kb_config.toml")
        return 1
    summary = _apply_ocr_mode(path, "local", "paddle")
    print(f"已关闭云端 OCR：{summary}")
    return 0


def cmd_ocr_keys(args):
    for provider in ("paddle", "mineru", "baidu", "openai"):
        envs = _key_envs(provider)
        for k in envs:
            scopes = _env_scopes(k)
            status = (
                f"进程:{'✓' if scopes['process'] else '✗'} "
                f"用户:{'✓' if scopes['user'] else '✗'} "
                f"系统:{'✓' if scopes['machine'] else '✗'}"
            )
            print(f"{provider} {k}: {status}")
            if not scopes["process"] and (scopes["user"] or scopes["machine"]):
                print(f"  提示: {k} 已在用户/系统作用域检测到，请重开终端生效，"
                      f"或先运行 $env:{k}='<你的key>'")
    return 0


def _env_scopes(name: str) -> dict[str, bool]:
    """检查环境变量在进程 / Windows 用户 / Windows 系统三个作用域是否存在。"""
    scopes = {"process": bool(os.environ.get(name)), "user": False, "machine": False}
    if os.name != "nt":
        return scopes
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            try:
                winreg.QueryValueEx(key, name)
                scopes["user"] = True
            except FileNotFoundError:
                pass
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            try:
                winreg.QueryValueEx(key, name)
                scopes["machine"] = True
            except FileNotFoundError:
                pass
    except Exception:
        pass
    return scopes


def cmd_scan(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.inspect import run_scan
    run_scan(cfg, state_only=args.state_only, milvus_only=args.milvus_only, logger=log)
    return 0


def cmd_setup(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.setup import run_setup
    return run_setup(cfg, logger=log)


def cmd_dedupe(args):
    cfg = _config(args)
    log = setup_logging()
    dry_run = not args.execute
    if args.replace_existing:
        cfg.replace_existing_md = "always"
    from kbimporter.dedupe import run_dedupe
    run_dedupe(cfg, dry_run=dry_run, scope=args.scope, logger=log)
    return 0


def cmd_search(args):
    cfg = _config(args)
    log = setup_logging()
    from kbimporter.models import get_client
    client = get_client(cfg)
    coll_name = args.collection
    if not client.has_collection(collection_name=coll_name):
        print(f"集合不存在: {coll_name}")
        return 2
    client.load_collection(collection_name=coll_name)
    output_fields = ["text", "source_file", "granularity", "parent_id", "chunk_index"]
    extra = {
        "academic_library": ["author", "year", "title", "language"],
        "fieldwork_kb": ["project_name", "source_type", "location"],
    }.get(coll_name, ["project_name", "author", "year", "title"])
    output_fields += [f for f in extra if f not in output_fields]
    if args.kind == "query":
        if not args.filter:
            print("query 模式需要 --filter")
            return 2
        results = client.query(
            collection_name=coll_name,
            filter=args.filter,
            output_fields=output_fields,
            limit=args.limit,
        )
        for r in results:
            print("=" * 60)
            for k in output_fields:
                if k in r:
                    print(f"{k}: {r[k]}")
        return 0
    if args.kind == "bm25":
        results = client.search(
            collection_name=coll_name,
            data=[args.query],
            anns_field="sparse",
            search_params={"metric_type": "BM25"},
            limit=args.limit,
            output_fields=output_fields,
            filter=args.filter or "",
        )
    else:
        results = client.search(
            collection_name=coll_name,
            data=[args.query],
            anns_field="vector",
            search_params={"metric_type": "IP"},
            limit=args.limit,
            output_fields=output_fields,
            filter=args.filter or "",
        )
    for i, hit in enumerate(results[0], 1):
        print("=" * 60)
        print(f"[{i}] 相似度: {hit.get('distance', 0.0):.4f}")
        entity = hit.get("entity", {})
        for k in output_fields:
            if k in entity:
                print(f"{k}: {entity[k]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb",
        description="知识库导入程序：Zotero 同步 / 文档转 Markdown / 去重清理 / Milvus 向量化导入",
    )
    parser.add_argument("--version", action="version", version=f"kbimporter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="生成配置模板")
    p.add_argument("--root", help="知识库根目录")
    p.add_argument("--output", "-o", help="输出配置文件路径")
    p.add_argument("--force", action="store_true", help="覆盖已有配置")
    p.add_argument("--interactive", action="store_true", help="交互式引导（默认在终端中自动启用）")
    p.add_argument("--non-interactive", action="store_true", help="关闭交互提示")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="查看知识库与增量状态（只读）")
    p.add_argument("--config", "-c")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("import", help="增量向量化导入（Milvus）")
    p.add_argument("--config", "-c")
    p.add_argument("--dry-run", action="store_true", help="只读预演，不写状态库、不调用 Milvus")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("sync-zotero", help="同步 Zotero storage 到文献库")
    p.add_argument("--config", "-c")
    p.add_argument("--dry-run", action="store_true", help="只读预演")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("convert", help="文档转 Markdown（Marker + MarkItDown）")
    p.add_argument("--config", "-c")
    p.add_argument("--dry-run", action="store_true", help="只读预演，不运行转换工具")
    p.add_argument("--scan-dir", help="覆盖扫描目录")
    p.add_argument("--engine", choices=["auto", "marker", "mineru", "cloud"],
                   default="auto", help="强制指定 PDF 引擎（auto=按配置回退链）")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("help", help="查看帮助")
    p.add_argument("command", nargs="?", help="具体命令名，如 kb help import")
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("ocr", help="管理 OCR 模式与云端 OCR")
    ocr_sub = p.add_subparsers(dest="ocr_command", required=True)
    ps = ocr_sub.add_parser("status", help="查看当前 OCR 模式与密钥状态")
    ps.add_argument("--config", "-c")
    ps.set_defaults(func=cmd_ocr_status)
    pm = ocr_sub.add_parser("mode", help="切换模式：local（本地）/ hybrid（混合）/ cloud（云端）")
    pm.add_argument("mode", choices=["local", "hybrid", "cloud"])
    pm.add_argument("priority", nargs="?", choices=["local", "cloud"],
                    help="hybrid 模式的优先顺序：local=本地优先（默认）/ cloud=云端优先")
    pm.add_argument("--provider",
                    choices=["paddle", "mineru", "baidu", "openai"],
                    default="paddle")
    pm.add_argument("--fallback",
                    choices=["paddle", "mineru", "baidu", "openai", "none"],
                    help="云端 OCR 备选 provider（默认 mineru；none 表示不设备选）")
    pm.add_argument("--config", "-c")
    pm.set_defaults(func=cmd_ocr_mode)
    pe = ocr_sub.add_parser("enable", help="启用云端 OCR（混合模式，本地失败自动云端）")
    pe.add_argument("--provider",
                    choices=["paddle", "mineru", "baidu", "openai"],
                    default="paddle")
    pe.add_argument("--fallback",
                    choices=["paddle", "mineru", "baidu", "openai", "none"],
                    help="云端 OCR 备选 provider（默认 mineru；none 表示不设备选）")
    pe.add_argument("--config", "-c")
    pe.set_defaults(func=cmd_ocr_enable)
    pd = ocr_sub.add_parser("disable", help="关闭云端 OCR（本地模式）")
    pd.add_argument("--config", "-c")
    pd.set_defaults(func=cmd_ocr_disable)
    pk = ocr_sub.add_parser("keys", help="查看各云端 OCR 密钥是否已设置")
    pk.add_argument("--config", "-c")
    pk.set_defaults(func=cmd_ocr_keys)

    p = sub.add_parser("doctor", help="扫描本机依赖/引擎/密钥/Milvus（默认只读）")
    p.add_argument("--config", "-c")
    p.add_argument("--deep", action="store_true",
                   help="端到端嵌入体检：临时创建并删除 _probe_kbimporter 集合（写操作）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("scan", help="扫描状态文件与 Milvus 库（只读）")
    p.add_argument("--config", "-c")
    p.add_argument("--state-only", action="store_true", help="只扫描状态文件")
    p.add_argument("--milvus-only", action="store_true", help="只扫描 Milvus 库")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("setup", help="引导创建虚拟环境并安装依赖")
    p.add_argument("--config", "-c")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("dedupe", help="同名/重复文件清理与 MD 替换")
    p.add_argument("--config", "-c")
    p.add_argument("--execute", action="store_true", help="实际执行（默认仅预演）")
    p.add_argument("--scope", choices=["project", "library", "all"], default="all")
    p.add_argument("--replace-existing", action="store_true",
                   help="强制用 Zotero 库 MD 替换现有 MD（含未知来源）")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("search", help="检索 Milvus（只读）")
    p.add_argument("--config", "-c")
    p.add_argument("--collection", required=True, help="集合名，如 academic_library / proj_xxx / fieldwork_kb")
    p.add_argument("--kind", choices=["dense", "bm25", "query"], default="dense")
    p.add_argument("query", nargs="?", help="查询文本（query 模式可省略）")
    p.add_argument("--filter", help="filter_expr，如 year >= 2020")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
