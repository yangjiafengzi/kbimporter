# AGENTS.md

本文件面向维护者、贡献者与 AI Agent：包含设计思想、完整配置参考、命令细节、
开发测试流程与安全规则。面向普通用户的精简说明见 [README.md](README.md)。

## 一、设计思想

- 本地 Zotero 是文献的终极源头，文件名即文献身份。
- 所有原始文献最终统一为 Markdown，再进入向量化。
- 项目文献先人工筛选，再优先从文献库复制同名 MD，避免重复 OCR。
- “中文比例最低 = 原文”只用于“英文原文 + 中文译本”场景。
- 增量导入：hash 对比，只处理新增/修改，删除只清对应 `source_file`。
- 安全第一：任何删除/替换都先移入回收目录，破坏性命令默认 dry-run。

## 二、与旧系统对应关系

| 新命令 | 替代的旧脚本 |
| --- | --- |
| `kb import` | `0向量化/` 全部（main/config/models/scanner/chunker/importer） |
| `kb sync-zotero` | `zotero文献库/main.py` |
| `kb convert` | `ocr/pdf_to_md.py` |
| `kb dedupe` | `项目文献/clean_duplicates.py` + `zotero文献库/remove_duplicate_pdfs.py` |
| `kb search` | 原 DB_GUIDE 中的 Milvus 检索示例（只读） |

相比旧脚本的改进：

- 路径、模型、批大小、超时全部配置化，不再硬编码。
- API 密钥只从环境变量读取（旧 OCR 脚本曾明文存放密钥）。
- 所有删除改为移入回收目录（默认 `<知识库>/.kb/trash`）。
- OCR 转换前检查同名 MD 已存在则跳过，避免重复 OCR。
- 项目文献替换时，本程序生成的 OCR MD 会被知识库内 `zotero文献库/library/` 中同名 MD
  顶替（该目录中的 MD 本身也多为 OCR 产物，单向替换合理；旧文件进回收目录）。
- 每条命令都支持 dry-run 预演。
- 空项目集合只删真正为空的，防止状态缺失时误删有数据集合。

## 三、模块结构

```text
src/kbimporter/
├── cli.py         # 命令入口（argparse）
├── config.py      # 配置模型与加载（TOML + 环境变量）
├── config_edit.py # 配置写入（section.key 行级编辑）
├── util.py        # 哈希/编码/回收目录/日志
├── chunker.py     # 双层切片
├── scanner.py     # 扫描/分类/增量状态/SQLite
├── models.py      # Milvus Schema 与集合管理（MilvusClient API）
├── importer.py    # 增量导入编排
├── zotero_sync.py # Zotero storage -> 文献库
├── convert.py     # MarkItDown + marker/mineru/cloud 引擎链
├── cloud_ocr.py   # 云端 OCR：paddle / mineru / baidu / openai（异步任务/分批/断点/回退链）
├── dedupe.py      # 去重/替换/清理
├── inspect.py     # 状态文件与 Milvus 集合扫描
├── doctor.py      # 环境体检（多 Python 环境探测）
└── setup.py       # 安装引导
```

## 四、配置参考

配置文件为 `kb_config.toml`（模板见 `kb_config.example.toml`，`kb init` 可生成）。

### `[paths]`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `kb_root` | 必填/`KB_ROOT` | 知识库根目录 |
| `library_dir` | `<kb_root>/zotero文献库/library` | Zotero 文献库 |
| `project_root` | `<kb_root>/项目文献` | 项目文献 |
| `fieldwork_root` | `<kb_root>/田野调查笔记` | 田野调查笔记 |
| `zotero_storage` | `~/Zotero/storage` | Zotero 附件源 |
| `state_dir` | `<kb_root>/.kb` | 状态目录 |
| `state_db` | `<state_dir>/state.db` | 增量状态库（可直接指向旧版 import_state.db 复用） |
| `trash_dir` | `<state_dir>/trash` | 回收目录 |
| `scan_dir` | `<kb_root>` | convert 扫描目录 |
| `ocr_work_dir` | `<kb_root>/ocr/_convert_work` | 转换工作目录 |
| `ocr_log_file` | `<state_dir>/logs/convert.log` | 转换日志（追加） |

### `[milvus]`

`host` / `port` / `embedding_provider` / `embedding_model` / `embedding_dim` /
`hnsw_m` / `hnsw_ef_construction` / `batch_size`。

向量由 Milvus 服务端生成：集合 Function 使用 `provider=dashscope` +
`model=text-embedding-v3`，密钥配置在 Milvus 服务端，本机不需要。

### `[chunk]`

`coarse_size=8192` / `coarse_overlap=1024` / `fine_size=1024` /
`fine_overlap=256` / `separators=["\n\n","\n","。","！","？","；","，"," "]`。

### `[converter]`

`marker_cmd` / `marker_single_cmd` / `markitdown_cmd` / `marker_workers` /
`max_per_batch` / `timeout_per_pdf` / `timeout_retry_pdf` / `engines` /
`mineru_cmd` / `mineru_backend` / `mineru_method` / `mineru_model_source` /
`enable_llm` / `llm_base_url` / `llm_model` / `after_convert` /
`skip_existing_md` / `skip_dirs` / `skip_exts` / `markitdown_exts`。

`engines` 默认 `["marker","mineru","cloud"]`；`cloud` 仅在 `cloud_ocr.enabled=true`
时生效。

### `[sync]`

`target_extensions = [".pdf",".epub"]`。

### `[dedupe]`

`supported_extensions = [".pdf",".epub"]`；
`replace_existing_md = "ocr_only" | "always" | "never"`。

### `[cloud_ocr]`

`enabled`（默认 false，必须显式开启）/ `provider`（`paddle` 首选 /
`mineru` / `baidu` / `openai`）/ `fallback_providers`（主 provider 失败后
依次尝试的备选，如 `["mineru"]`）/ `state_dir`。

#### `[cloud_ocr.paddle]`

PaddleOCR 云端异步任务 API：

```toml
job_url = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
model = "PaddleOCR-VL-1.6"
api_key_env = "PADDLE_OCR_API_KEY"
poll_interval = 5
max_poll_seconds = 7200
max_pages_per_task = 100
```

流程：超过 `max_pages_per_task` 页时先按页拆分子 PDF -> 各子任务 multipart 提交 ->
轮询 jobId -> 下载 JSONL -> 解析 `layoutParsingResults[].markdown.text` ->
按页缓存并按页序合并。断点续传：已完成子任务不重复提交。

#### `[cloud_ocr.mineru]`

MinerU 精准解析云 API（token 从 `api_key_env` 指定的环境变量读取）：

```toml
upload_url = "https://mineru.net/api/v4/file-urls/batch"
result_url = "https://mineru.net/api/v4/extract-results/batch"
api_key_env = "MINERU_API_KEY"
model_version = "vlm"   # pipeline / vlm（推荐）/ MinerU-HTML
is_ocr = true
enable_formula = true
enable_table = true
language = "ch"
poll_interval = 5
max_poll_seconds = 7200
max_pages_per_task = 200
```

流程：申请上传链接 -> PUT 上传整份 PDF -> 轮询 `extract-results/batch/{batch_id}`
-> 下载结果 zip 并解出 `full.md`。超过 `max_pages_per_task` 页时自动拆分多个
子任务，识别完成后按页序合并；断点续传，已完成子任务不重复提交。

#### `[cloud_ocr.baidu]`

百度智能云 `accurate_basic`，OAuth token + 每页图片请求，支持并发。

#### `[cloud_ocr.openai]`

OpenAI 兼容视觉接口（默认 DashScope qwen3-vl-plus），每请求可包含多页图片。

## 五、命令详细说明

所有命令支持 `--config <路径>`（默认读取当前目录 `kb_config.toml`），
也可用 `KB_ROOT` 环境变量指定知识库根目录。

### `kb init`

```bash
kb init --root <知识库路径> [--output <配置路径>] [--interactive] [--force]
```

生成 `kb_config.toml` 并创建 `zotero文献库/library`、`项目文献`、
`田野调查笔记`、`.kb` 目录。终端交互模式询问显卡并推荐 OCR 方案。

### `kb status` / `kb scan` / `kb doctor`

三者默认均只读。`doctor` 会探测当前解释器之外的其他 Python 环境
（miniconda base、conda envs、系统 Python），报告依赖可复用情况；
环境变量提示按 `cloud_ocr.provider` 精准给出。
`kb doctor --deep` 是显式写操作：临时创建并删除 `_probe_kbimporter` 集合，
用于端到端嵌入体检，只碰该临时集合。

### `kb setup`

先运行 doctor 体检，再询问显卡与 OCR 方案（本地 / 云端 / 混合），
按方案安装对应 extras：核心 `[import,sync,dedupe]` 始终安装，本地/混合
加 `[ocr]`，云端/混合加 `[cloud]`；随后按需创建、复用虚拟环境或补装
依赖，可自动写入配置。非交互模式只打印按方案安装的命令提示。

### `kb import`

增量导入，非 dry-run 时写状态库并调用 Milvus。dry-run 只读：
不写状态库、不调用 Milvus。首次使用旧状态库时可直接把 `state_db`
指向旧 `import_state.db` 复用（程序不会给旧库加表）。

### `kb sync-zotero`

按基础名分组，选中文比例最低的版本（原版）复制到文献库；清理过期记录。
删除操作进回收目录。

### `kb convert`

引擎链顺序由 `converter.engines` 决定；已存在同名 MD 的文件默认跳过。
`--engine marker|mineru|cloud` 可强制指定。

### `kb ocr`

```bash
kb ocr status
kb ocr mode local|hybrid [local|cloud]|cloud [--provider paddle|mineru|baidu|openai] [--fallback mineru|none]
kb ocr enable [--provider paddle] [--fallback mineru]
kb ocr disable
kb ocr keys
```

写入配置：`converter.engines` 与 `cloud_ocr.enabled/provider/fallback_providers`。
`hybrid local` 写入 `engines=["marker","mineru","cloud"]`（本地优先）；
`hybrid cloud` 写入 `engines=["cloud","marker","mineru"]`（云端优先，云端失败后
用 marker_single / mineru 本地兜底）。`hybrid` 不带优先级默认本地优先。

### `kb dedupe`

默认只预演；`--execute` 才实际执行；`--scope project|library|all`；
`--replace-existing` 强制用文献库 MD 顶替现有 MD。替换前按
`file_origin`（`zotero_md` / `ocr_md` / 未知）决定是否覆盖。

### `kb search`

```bash
kb search --collection <集合名> --kind dense|bm25|query <查询词> [--filter <expr>]
```

## 六、状态库与来源记录

状态库表：

- `file_state`：文件路径、hash、status、collection、chunk_count
- `project_meta_state`：田野项目元数据 hash
- `file_origin`：MD 来源（`zotero_md` / `ocr_md`），用于替换策略

复用旧版 `0向量化/import_state.db`：把 `[paths].state_db` 指向该文件即可；
程序不会迁移、不会新增表、不会修改其结构（缺 `file_origin` 时自动降级为未知来源）。

## 七、开发与测试

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests -q
```

测试全部使用临时目录与 Mock（`tests/conftest.py` 提供假 pymilvus），
不触碰真实知识库与 Milvus。

打包：

```bash
pip install build
python -m build
```

产物在 `dist/`。

## 八、对 Agent 的行为规则

1. 不得修改、删除、迁移用户的旧状态库（`0向量化/import_state.db`）与 Milvus 集合。
2. 任何真实导入/转换/清理前必须先 dry-run，并向用户说明费用与影响。
3. 删除/替换必须先移入回收目录，不直接 `rm`。
4. 云端 OCR 默认关闭；只有用户显式确认后才启用。
5. 密钥只从环境变量读取，禁止写入代码或配置。
6. 测试不得连接真实 Milvus 或读取真实知识库数据。
7. 不碰无关集合：只处理用户指定的集合，绝不顺带动其他集合。
8. 最小化影响：能增量更新就不全量重建，能重建一个集合就不重建全部。
9. 事故案例（2026-06-03）：某 Agent 仅需给 `proj_cunganbuleixing` 添加字段，
   却擅自运行 `_rebuild.py`，导致三个集合全部被删除重建，造成 DashScope API 费用损失
   与大量时间浪费。任何重建类操作都必须先获得用户明确许可。

数据库结构详细说明（Schema、父子块、索引、检索与 filter 语法）见
[docs/DB_GUIDE.md](docs/DB_GUIDE.md)；Agent 提示词示例见 [agents/](agents/)。
