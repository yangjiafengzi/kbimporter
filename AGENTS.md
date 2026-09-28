# AGENTS.md

面向维护者、贡献者与 AI Agent。普通用户说明见 [README.md](README.md)；
数据库 Schema / 检索语法见 [docs/DB_GUIDE.md](docs/DB_GUIDE.md)。

## 设计思想

- 本地 Zotero 是文献终极源头，**文件名即文献身份**（`作者 - 年份 - 标题.md`）。
- 一切原始文献先转 Markdown，再进入向量化。
- 项目文献人工筛选后，优先复用文献库同名 MD，避免重复 OCR。
- “中文比例最低 = 原文”只用于“英文原文 + 中文译本”；无文字层 PDF 不参与该判定。
- 增量导入：hash 对比，只处理新增/修改；删除只清对应 `source_file`。
- 删除/替换一律先移入回收目录（默认 `<知识库>/.kb/trash`），不直接 `rm`。
- 向量由 **Milvus 服务端**生成（DashScope Function），本机不跑嵌入模型。

## 架构

```text
src/kbimporter/
├── cli.py         # argparse 入口（kb -> cli:main）
├── config.py      # 配置模型与加载（TOML + 环境变量）
├── config_edit.py # section.key 行级写入配置
├── util.py        # 哈希 / 编码 / 回收目录 / 日志
├── chunker.py     # 双层切片（coarse/fine，按 UTF-8 字节长度）
├── scanner.py     # 扫描分类、增量状态、SQLite
├── models.py      # Milvus Schema / 集合管理（MilvusClient）
├── importer.py    # 增量导入编排
├── zotero_sync.py # Zotero storage -> 文献库
├── convert.py     # MarkItDown + marker/mineru/cloud 引擎链
├── cloud_ocr.py   # 云端 OCR：paddle / mineru / baidu / openai
├── dedupe.py      # 去重 / 替换 / 清理
├── inspect.py     # 状态库与 Milvus 扫描
├── doctor.py      # 环境体检（多 Python 环境探测）
├── progress.py    # 终端进度面板（线程安全）
└── setup.py       # 安装引导
```

三库分构（集合互不污染）：

| 来源 | 目录 | 集合 | 特有字段 |
| --- | --- | --- | --- |
| Zotero 文献库 | `zotero文献库/library/` | `academic_library` | `language, author, year, title` |
| 项目文献 | `项目文献/<项目名>/` | `proj_<项目名拼音>` | `project_name, language, author, year, title` |
| 田野笔记 | `田野调查笔记/<项目>/` | `fieldwork_kb` | `source_type, source_path, project_name, location, research_date, researchers, notes` |

共享字段：`id, text, source_file, chunk_index, granularity, parent_id, vector, sparse, created_at`。
`source_file` 是增量更新/删除的唯一锚点（相对知识库根）。
`_项目信息.md` 不切片，只解析为田野元数据，改动后 upsert 该项目全部记录。

`academic_library` 另有**语言×年代手工分区**（`{zh|en}_{pre1980|1980s|…|2050s|unknown}`，20 个）。
检索应用 `--year-from/to` 或 `--partitions` 只 `load_partitions`，禁止无脑整库 `load_collection`。
历史数据用 `kb repartition academic-library`（默认 dry-run，`--execute` 执行）向量保真搬迁，**0 次嵌入调用**。
Agent 提示词模板（示例 + 实际使用）见 [agents/partition_loading_prompts.md](agents/partition_loading_prompts.md)。
详见 [docs/DB_GUIDE.md](docs/DB_GUIDE.md) 分区专节。

## 配置

`kb_config.toml`（模板 `kb_config.example.toml`，`kb init` 生成）。
查找顺序：`--config` > `KB_CONFIG` > 当前目录 `kb_config.toml` > 项目根 >
`%APPDATA%\kbimporter`（macOS/Linux：`~/.config/kbimporter`）。
`--config` 是全局参数，放在子命令前后均可。

完整键与注释以 `kb_config.example.toml` 为准。必须知道的点：

- **密钥只从环境变量读**，禁止写入代码或配置。`api_key_env` 指定变量名。
- **两组 DashScope 配置不要混**：
  - Milvus 向量化：服务端 `MILVUSAI_DASHSCOPE_API_KEY`（或 `deploy/user.yaml`），
    本机**不需要** `DASHSCOPE_API_KEY`。自定义端点必须用原生
    `/api/v1/services/embeddings/text-embedding/text-embedding`，
    不能用 OpenAI `compatible-mode/v1`（否则 404 或 `embedding:[0]`）。
  - 云端 OCR（`openai` provider）：本机 `DASHSCOPE_API_KEY` + compatible-mode base_url。
- `[paths].state_db` 可直接指向旧版 `0向量化/import_state.db`：程序不迁移、不加表、
  不改结构；缺 `file_origin` 表时自动降级为未知来源。
- `[converter].engines` 默认 `["marker","mineru","cloud"]`；`cloud` 仅在
  `cloud_ocr.enabled=true` 时生效。
- `cloud_ocr.enabled` 默认 **false**。云端 OCR 产生费用且文档会出网，只有用户显式确认后才启用。
- `[dedupe].replace_existing_md`：`ocr_only`（默认，只顶替本程序记录的 OCR 产物）/
  `always` / `never`。未知来源 MD 默认不覆盖。
- Python 3.13+ 下 `[ocr]` 重型依赖（PyTorch 等）可能无预编译 wheel；本地 OCR 建议 3.11/3.12。

### 云端 OCR 行为（改 `cloud_ocr.py` 时必须保持）

- 429 分两种：`code=12002` / “请求频率过高”是**限流**，退避重试，不换 provider；
  “额度已用完 / 配额不足 / 余额不足”或 MinerU `-60018` 才是**额度耗尽**，
  抛 `CloudQuotaError` 熔断并切 `fallback_providers`，本次运行剩余文件跳过该 provider。
- 其他错误（500/503/504 等）一律先退避重试，耗尽后才回退。
- 超过 `max_pages_per_task`（paddle 100 / mineru 200）自动拆子 PDF，按页序合并；
  已完成子任务断点续传，不重复提交。
- 子任务按 `max_workers`（默认 5）持续并发；失败/额度耗尽取消未启动任务。
  `stall_timeout`（默认 900s）进度不增长视为卡死并重新提交。

## 命令

```text
kb init --root <路径> [--output <配置>] [--interactive|--non-interactive] [--force]
kb status | kb doctor [--deep] | kb scan [--state-only|--milvus-only] | kb setup
kb sync-zotero [--dry-run]
kb convert [--dry-run] [--scan-dir <目录>] [--engine auto|marker|mineru|cloud]
kb import [--dry-run]
kb dedupe [--dry-run] [--scope project|library|all] [--replace-existing]
kb ocr status|mode|enable|disable|keys
kb release [集合名] [--partitions zh_2010s,...]
kb search --collection <集合> --kind dense|bm25|query [<词>] \
  [--partitions ...] [--year-from Y --year-to Y] [--lang zh|en] \
  [--filter <expr>] [--limit N] [--release]
kb repartition academic-library [--execute] [--batch-size N]
kb shell-init [--apply]
kb help [命令]
```

非显而易见的行为：

- **写操作不会默认 dry-run**。`import` / `sync-zotero` / `convert` 的 `--dry-run`
  需显式加；`kb dedupe` **默认直接执行**（文件进回收目录）。Agent 在真实导入/转换前
  必须先 dry-run 并说明影响。
- `status` / `scan` / `doctor`（无 `--deep`）/ `search` / `release` 只读。
  `doctor --deep` 是写操作：临时建删 `_probe_kbimporter`，只碰该集合。
- `kb release [集合]` 只卸载 Milvus 内存，**不删数据**；不填集合名释放全部。
  `kb search` 默认保留加载以便连续检索，`--release` 检索后释放。
- `kb init` 会读 Zotero `prefs.js` 的 `extensions.zotero.dataDir`，自动修正
  `zotero_storage`（否则同步出空目录）；`doctor`/`status` 对不一致给出警告。
- `kb convert`：同名 MD 已存在则跳过（`skip_existing_md`）；`--engine` 强制单引擎。
  TTY 下有实时进度面板，管道/重定向自动退化为普通日志。
- `kb sync-zotero`：按基础名分组选中文比例最低版本；无文字层 PDF 标记为扫描件，
  不参与“中文比例最低=原文”，同步结束输出扫描件清单。
- `kb ocr enable --provider paddle --fallback mineru` = hybrid local（推荐）；
  `hybrid cloud` 引擎链为 `cloud -> marker -> mineru`。写入 `converter.engines` 与
  `cloud_ocr.*`。`kb ocr keys` 在 Windows 查注册表，Unix 读 shell 配置文件中的 export。
- `kb shell-init --apply` 幂等写入 shell 启动项；macOS/Linux 的软链只打印不自动执行。
- `kb dedupe --execute` 已废弃（会被忽略并警告）。
- 空项目集合只删真正为空的 `proj_*`（状态库无记录且 `row_count==0`），防误删。
- `kb search --year-from/--year-to/--partitions` 只加载 academic_library 对应分区；
  `kb release --partitions` 卸载子集。`kb repartition` 默认 dry-run，`--execute` 才搬迁。
  迁移时 `delete` 必须限定 `partition_name="_default"`，否则会误删新分区数据。

## 状态库（SQLite）

| 表 | 用途 |
| --- | --- |
| `file_state` | 路径、hash、status、collection、chunk_count |
| `project_meta_state` | 田野项目元数据 hash |
| `file_origin` | MD 来源（`zotero_md` / `ocr_md`），驱动替换策略 |

复用旧 `import_state.db` 时程序不会迁移或改表；缺 `file_origin` 自动降级。

## 开发与测试

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"          # macOS/Linux: .venv/bin/pip
.venv\Scripts\python -m pytest tests -q
.venv\Scripts\python -m pytest tests/test_cli.py::test_cli_help_command -q   # 单测
```

- 测试**不连接真实 Milvus、不读真实知识库**。`tests/conftest.py` 注入假 pymilvus
  并跳过 TCP 预检；pymilvus 可不装。PDF/拼音相关用例需要对应 extras（懒导入）。
- 打包：`pip install build && python -m build`，产物在 `dist/`。
- 无 lint/typecheck 工具链，验证以 pytest 为准。
- 发行包不捆绑第三方依赖，按 extras 装：`[import,sync,dedupe]` 核心；
  `[ocr]` 本地转换（含 PyTorch）；`[cloud]` 云端 OCR；`[search]` 检索。
- `agents/` 是可分发的 Agent 提示词范例；`agents_nopush/` 本地专用且被 gitignore。

## Agent 行为规则

1. **不得**修改、删除、迁移用户的旧状态库（`0向量化/import_state.db`）与既有 Milvus 集合结构。
2. 真实导入/转换前必须先 `--dry-run`，并向用户说明费用与影响。
   `kb dedupe` 默认直接执行，需要预演时先 `kb dedupe --dry-run`。
3. 删除/替换必须先移入回收目录，不直接 `rm` / 清空集合。
4. 云端 OCR 默认关闭；只有用户显式确认后才启用。
5. 密钥只从环境变量读取，禁止写入代码或配置文件。
6. 测试不得连接真实 Milvus 或读取真实知识库数据。
7. 不碰无关集合：只处理用户指定的集合。
8. 最小化影响：能增量就不全量重建；能重建一个集合就不重建全部。
9. **事故案例（2026-06-03）**：某 Agent 只需给 `proj_cunganbuleixing` 添加字段，
   却擅自运行 `_rebuild.py`，导致三个集合全部删除重建，造成 DashScope API 费用损失
   与大量时间。**任何重建/删集合类操作都必须先获得用户明确许可。**
