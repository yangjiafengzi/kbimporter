from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_CONFIG_FILE = "kb_config.toml"
CONFIG_ENV = "KB_CONFIG"

DEFAULT_CLOUD_OCR_PROMPT = (
    "你是一位专业的 OCR 识别专家。请识别图片中的全部文字，"
    "严格保持段落和排版结构，输出 Markdown 文本；"
    "忽略页眉、页脚、页码；模糊之处标注[?]；不要输出任何解释。"
)


@dataclass
class ChunkConfig:
    coarse_size: int = 8192
    coarse_overlap: int = 1024
    fine_size: int = 1024
    fine_overlap: int = 256
    separators: list[str] = field(
        default_factory=lambda: ["\n\n", "\n", "。", "！", "？", "；", "，", " "]
    )


@dataclass
class MilvusConfig:
    host: str = "localhost"
    port: str = "19530"
    embedding_provider: str = "dashscope"
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024
    hnsw_m: int = 16
    hnsw_ef_construction: int = 256
    batch_size: int = 30


@dataclass
class OpenAICompatibleOCR:
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3-vl-plus"
    api_key_env: str = "DASHSCOPE_API_KEY"
    page_batch_size: int = 1
    max_workers: int = 4
    scale_factor: float = 3.0
    timeout: int = 120
    max_retries: int = 3
    prompt: str = DEFAULT_CLOUD_OCR_PROMPT


@dataclass
class BaiduOCR:
    token_url: str = "https://aip.baidubce.com/oauth/2.0/token"
    accurate_url: str = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
    api_key_env: str = "BAIDU_OCR_API_KEY"
    secret_key_env: str = "BAIDU_OCR_SECRET_KEY"
    language_type: str = "CHN_ENG"
    detect_direction: bool = True
    paragraph: bool = True
    scale_factor: float = 2.0
    max_workers: int = 4
    timeout: int = 60
    max_retries: int = 3


@dataclass
class PaddleCloudOCR:
    """PaddleOCR 云端异步任务 API（aistudio-app）。"""
    job_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    model: str = "PaddleOCR-VL-1.6"
    api_key_env: str = "PADDLE_OCR_API_KEY"
    timeout: int = 120
    max_retries: int = 3
    poll_interval: int = 5
    max_poll_seconds: int = 7200
    max_pages_per_task: int = 100
    max_workers: int = 5
    stall_timeout: int = 900
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_chart_recognition: bool = False


@dataclass
class MineruCloudOCR:
    """MinerU 精准解析云 API（mineru.net/apiManage/docs）。

    流程：申请上传链接 -> PUT 上传 PDF -> 轮询批量结果 -> 下载 zip 并取出 full.md。
    """
    upload_url: str = "https://mineru.net/api/v4/file-urls/batch"
    result_url: str = "https://mineru.net/api/v4/extract-results/batch"
    api_key_env: str = "MINERU_API_KEY"
    model_version: str = "vlm"  # pipeline / vlm（推荐）/ MinerU-HTML
    is_ocr: bool = True
    enable_formula: bool = True
    enable_table: bool = True
    language: str = "ch"
    timeout: int = 120
    max_retries: int = 3
    poll_interval: int = 5
    max_poll_seconds: int = 7200
    max_pages_per_task: int = 200
    max_workers: int = 5
    stall_timeout: int = 900


@dataclass
class CloudOCRConfig:
    enabled: bool = False
    provider: str = "paddle"  # paddle（首选，PaddleOCR 云 API） | mineru | baidu | openai
    fallback_providers: list[str] = field(default_factory=list)
    state_dir: Path | None = None
    openai: OpenAICompatibleOCR = field(default_factory=OpenAICompatibleOCR)
    baidu: BaiduOCR = field(default_factory=BaiduOCR)
    paddle: PaddleCloudOCR = field(default_factory=PaddleCloudOCR)
    mineru: MineruCloudOCR = field(default_factory=MineruCloudOCR)


@dataclass
class Config:
    kb_root: Path | None = None
    library_dir: Path | None = None
    project_root: Path | None = None
    fieldwork_root: Path | None = None
    zotero_storage: Path | None = None
    hash_history_file: Path | None = None
    state_dir: Path | None = None
    state_db_path: Path | None = None
    trash_dir: Path | None = None
    scan_dir: Path | None = None
    ocr_work_dir: Path | None = None
    ocr_log_file: Path | None = None

    marker_cmd: str = "marker"
    marker_single_cmd: str = "marker_single"
    markitdown_cmd: str = "markitdown"
    marker_workers: int = 1
    max_per_batch: int = 20
    timeout_per_pdf: int = 900
    timeout_retry_pdf: int = 1800
    enable_llm: bool = False
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.5-plus"
    after_convert: str = "trash"
    skip_existing_md: bool = True
    skip_dirs: list[str] = field(
        default_factory=lambda: [
            "_convert_work", "ocr", "__pycache__", ".git", "0向量化", ".kb",
        ]
    )
    skip_exts: list[str] = field(
        default_factory=lambda: [".pdf", ".md", ".json", ".py", ".log"]
    )
    engines: list[str] = field(
        default_factory=lambda: ["marker", "mineru", "cloud"]
    )
    mineru_cmd: str = "mineru"
    mineru_backend: str = "hybrid-engine"
    mineru_method: str = "ocr"
    mineru_model_source: str = "auto"
    markitdown_exts: list[str] = field(
        default_factory=lambda: [
            ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
            ".epub", ".html", ".htm", ".csv", ".xml", ".txt", ".rtf", ".odt",
        ]
    )

    target_extensions: set[str] = field(default_factory=lambda: {".pdf", ".epub"})
    supported_extensions: set[str] = field(default_factory=lambda: {".pdf", ".epub"})
    replace_existing_md: str = "ocr_only"  # ocr_only | always | never

    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    milvus: MilvusConfig = field(default_factory=MilvusConfig)
    cloud_ocr: CloudOCRConfig = field(default_factory=CloudOCRConfig)

    dashscope_api_key: str = ""
    llm_api_key: str = ""
    config_path: Path | None = None

    @property
    def state_db(self) -> Path:
        if self.state_db_path:
            return self.state_db_path
        return (self.state_dir or Path(".kb")) / "state.db"

    def require_kb_root(self) -> Path:
        if not self.kb_root:
            raise ValueError(
                "未设置 kb_root：请在配置文件中填写 [paths].kb_root，"
                "或设置环境变量 KB_ROOT。"
            )
        return self.kb_root

    def derive(self) -> "Config":
        root = self.kb_root
        if root:
            if not self.library_dir:
                self.library_dir = root / "zotero文献库" / "library"
            if not self.project_root:
                self.project_root = root / "项目文献"
            if not self.fieldwork_root:
                self.fieldwork_root = root / "田野调查笔记"
            if not self.hash_history_file:
                self.hash_history_file = root / "zotero文献库" / "hash_history.json"
            if not self.state_dir:
                self.state_dir = root / ".kb"
            if not self.scan_dir:
                self.scan_dir = root
            if not self.ocr_work_dir:
                self.ocr_work_dir = root / "ocr" / "_convert_work"
            if not self.ocr_log_file:
                self.ocr_log_file = (self.state_dir or root / ".kb") / "logs" / "convert.log"
        if not self.trash_dir and self.state_dir:
            self.trash_dir = self.state_dir / "trash"
        if not self.cloud_ocr.state_dir and self.state_dir:
            self.cloud_ocr.state_dir = self.state_dir / "cloud_ocr"
        if not self.zotero_storage:
            self.zotero_storage = Path.home() / "Zotero" / "storage"
        return self

    def apply_env(self) -> "Config":
        if os.environ.get("KB_ROOT"):
            self.kb_root = Path(os.environ["KB_ROOT"])
        if os.environ.get("MILVUS_HOST"):
            self.milvus.host = os.environ["MILVUS_HOST"]
        if os.environ.get("MILVUS_PORT"):
            self.milvus.port = os.environ["MILVUS_PORT"]
        if os.environ.get("DASHSCOPE_API_KEY"):
            self.dashscope_api_key = os.environ["DASHSCOPE_API_KEY"]
        if os.environ.get("LLM_API_KEY"):
            self.llm_api_key = os.environ["LLM_API_KEY"]
        elif not self.llm_api_key:
            self.llm_api_key = self.dashscope_api_key
        if os.environ.get("LLM_API_BASE"):
            self.llm_base_url = os.environ["LLM_API_BASE"]
        if os.environ.get("LLM_MODEL"):
            self.llm_model = os.environ["LLM_MODEL"]
        if os.environ.get("MARKER_CMD"):
            self.marker_cmd = os.environ["MARKER_CMD"]
        if os.environ.get("MARKITDOWN_CMD"):
            self.markitdown_cmd = os.environ["MARKITDOWN_CMD"]
        return self

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        def _p(section: str, key: str, default=None):
            return raw.get(section, {}).get(key, default)

        cfg = cls()
        paths = raw.get("paths", {})
        if paths.get("kb_root"):
            cfg.kb_root = Path(str(paths["kb_root"]))
        if paths.get("library_dir"):
            cfg.library_dir = Path(str(paths["library_dir"]))
        if paths.get("project_root"):
            cfg.project_root = Path(str(paths["project_root"]))
        if paths.get("fieldwork_root"):
            cfg.fieldwork_root = Path(str(paths["fieldwork_root"]))
        if paths.get("zotero_storage"):
            cfg.zotero_storage = Path(str(paths["zotero_storage"]))
        if paths.get("state_dir"):
            cfg.state_dir = Path(str(paths["state_dir"]))
        if paths.get("state_db"):
            cfg.state_db_path = Path(str(paths["state_db"]))
        if paths.get("trash_dir"):
            cfg.trash_dir = Path(str(paths["trash_dir"]))
        if paths.get("scan_dir"):
            cfg.scan_dir = Path(str(paths["scan_dir"]))
        if paths.get("ocr_work_dir"):
            cfg.ocr_work_dir = Path(str(paths["ocr_work_dir"]))
        if paths.get("ocr_log_file"):
            cfg.ocr_log_file = Path(str(paths["ocr_log_file"]))

        milvus = raw.get("milvus", {})
        for key in ("host", "port", "embedding_provider", "embedding_model"):
            if milvus.get(key):
                setattr(cfg.milvus, key, str(milvus[key]))
        for key, cast in (("embedding_dim", int), ("hnsw_m", int),
                          ("hnsw_ef_construction", int), ("batch_size", int)):
            if milvus.get(key) is not None:
                setattr(cfg.milvus, key, cast(milvus[key]))

        chunk = raw.get("chunk", {})
        if chunk.get("coarse_size") is not None:
            cfg.chunk.coarse_size = int(chunk["coarse_size"])
        if chunk.get("coarse_overlap") is not None:
            cfg.chunk.coarse_overlap = int(chunk["coarse_overlap"])
        if chunk.get("fine_size") is not None:
            cfg.chunk.fine_size = int(chunk["fine_size"])
        if chunk.get("fine_overlap") is not None:
            cfg.chunk.fine_overlap = int(chunk["fine_overlap"])
        if chunk.get("separators"):
            cfg.chunk.separators = list(chunk["separators"])

        conv = raw.get("converter", {})
        for key in ("marker_cmd", "marker_single_cmd", "markitdown_cmd",
                    "llm_base_url", "llm_model", "after_convert"):
            if conv.get(key):
                setattr(cfg, key, str(conv[key]))
        for key, cast in (("marker_workers", int), ("max_per_batch", int),
                          ("timeout_per_pdf", int), ("timeout_retry_pdf", int)):
            if conv.get(key) is not None:
                setattr(cfg, key, cast(conv[key]))
        if conv.get("enable_llm") is not None:
            cfg.enable_llm = bool(conv["enable_llm"])
        if conv.get("skip_existing_md") is not None:
            cfg.skip_existing_md = bool(conv["skip_existing_md"])
        if conv.get("skip_dirs"):
            cfg.skip_dirs = list(conv["skip_dirs"])
        if conv.get("skip_exts"):
            cfg.skip_exts = list(conv["skip_exts"])
        if conv.get("engines"):
            cfg.engines = list(conv["engines"])
        if conv.get("mineru_cmd"):
            cfg.mineru_cmd = str(conv["mineru_cmd"])
        if conv.get("mineru_backend"):
            cfg.mineru_backend = str(conv["mineru_backend"])
        if conv.get("mineru_method"):
            cfg.mineru_method = str(conv["mineru_method"])
        if conv.get("mineru_model_source"):
            cfg.mineru_model_source = str(conv["mineru_model_source"])
        if conv.get("markitdown_exts"):
            cfg.markitdown_exts = list(conv["markitdown_exts"])

        sync = raw.get("sync", {})
        if sync.get("target_extensions"):
            cfg.target_extensions = set(sync["target_extensions"])

        dedupe = raw.get("dedupe", {})
        if dedupe.get("supported_extensions"):
            cfg.supported_extensions = set(dedupe["supported_extensions"])
        if dedupe.get("replace_existing_md"):
            cfg.replace_existing_md = str(dedupe["replace_existing_md"])

        cloud = raw.get("cloud_ocr", {})
        if cloud.get("enabled") is not None:
            cfg.cloud_ocr.enabled = bool(cloud["enabled"])
        if cloud.get("provider"):
            cfg.cloud_ocr.provider = str(cloud["provider"])
        if cloud.get("fallback_providers"):
            cfg.cloud_ocr.fallback_providers = [
                str(x) for x in cloud["fallback_providers"]
            ]
        if cloud.get("state_dir"):
            cfg.cloud_ocr.state_dir = Path(str(cloud["state_dir"]))
        oai = cloud.get("openai", {})
        for key in ("base_url", "model", "api_key_env", "prompt"):
            if oai.get(key):
                setattr(cfg.cloud_ocr.openai, key, str(oai[key]))
        for key, cast in (("page_batch_size", int), ("max_workers", int),
                          ("timeout", int), ("max_retries", int)):
            if oai.get(key) is not None:
                setattr(cfg.cloud_ocr.openai, key, cast(oai[key]))
        if oai.get("scale_factor") is not None:
            cfg.cloud_ocr.openai.scale_factor = float(oai["scale_factor"])
        bd = cloud.get("baidu", {})
        for key in ("token_url", "accurate_url", "api_key_env",
                    "secret_key_env", "language_type"):
            if bd.get(key):
                setattr(cfg.cloud_ocr.baidu, key, str(bd[key]))
        for key, cast in (("max_workers", int), ("timeout", int),
                          ("max_retries", int)):
            if bd.get(key) is not None:
                setattr(cfg.cloud_ocr.baidu, key, cast(bd[key]))
        for key in ("detect_direction", "paragraph"):
            if bd.get(key) is not None:
                setattr(cfg.cloud_ocr.baidu, key, bool(bd[key]))
        if bd.get("scale_factor") is not None:
            cfg.cloud_ocr.baidu.scale_factor = float(bd["scale_factor"])

        pdl = cloud.get("paddle", {})
        for key in ("job_url", "model", "api_key_env"):
            if pdl.get(key):
                setattr(cfg.cloud_ocr.paddle, key, str(pdl[key]))
        for key, cast in (("timeout", int), ("max_retries", int),
                          ("poll_interval", int), ("max_poll_seconds", int),
                          ("max_pages_per_task", int), ("max_workers", int),
                          ("stall_timeout", int)):
            if pdl.get(key) is not None:
                setattr(cfg.cloud_ocr.paddle, key, cast(pdl[key]))
        for key in ("use_doc_orientation_classify", "use_doc_unwarping",
                    "use_chart_recognition"):
            if pdl.get(key) is not None:
                setattr(cfg.cloud_ocr.paddle, key, bool(pdl[key]))

        mnr = cloud.get("mineru", {})
        for key in ("upload_url", "result_url", "api_key_env",
                    "model_version", "language"):
            if mnr.get(key):
                setattr(cfg.cloud_ocr.mineru, key, str(mnr[key]))
        for key, cast in (("timeout", int), ("max_retries", int),
                          ("poll_interval", int), ("max_poll_seconds", int),
                          ("max_pages_per_task", int), ("max_workers", int),
                          ("stall_timeout", int)):
            if mnr.get(key) is not None:
                setattr(cfg.cloud_ocr.mineru, key, cast(mnr[key]))
        for key in ("is_ocr", "enable_formula", "enable_table"):
            if mnr.get(key) is not None:
                setattr(cfg.cloud_ocr.mineru, key, bool(mnr[key]))
        return cfg


def load_config(path: str | Path | None = None) -> Config:
    """加载配置：TOML 文件 + 环境变量覆盖 + 派生路径。

    配置文件查找顺序：显式 --config > KB_CONFIG 环境变量 > 当前目录 >
    项目根（源码安装时） > 全局配置目录（%APPDATA%/kbimporter 或 ~/.config/kbimporter）。
    """
    cfg_path = discover_config_path(path)
    raw: dict = {}
    if cfg_path is not None and cfg_path.exists():
        with open(cfg_path, "rb") as f:
            raw = tomllib.load(f)
    cfg = Config.from_dict(raw)
    cfg.apply_env()
    cfg.derive()
    cfg.config_path = cfg_path
    return cfg


def discover_config_path(explicit: str | Path | None = None) -> Path | None:
    """按统一顺序查找配置文件；找不到返回 None。"""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None

    env_path = os.environ.get(CONFIG_ENV)
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    cwd_file = Path(DEFAULT_CONFIG_FILE)
    if cwd_file.exists():
        return cwd_file

    # 源码安装时包目录的上级可能是项目根
    pkg_parents = Path(__file__).resolve().parents
    for cand in (pkg_parents[2], pkg_parents[1]):
        p = cand / DEFAULT_CONFIG_FILE
        if p.exists():
            return p

    for d in _global_config_dirs():
        p = d / DEFAULT_CONFIG_FILE
        if p.exists():
            return p
    return None


def _global_config_dirs() -> list[Path]:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return [Path(appdata) / "kbimporter"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return [Path(xdg) / "kbimporter"]
    return [Path.home() / ".config" / "kbimporter"]
