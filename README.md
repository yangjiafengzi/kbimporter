# kbimporter — 知识库导入程序

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](pyproject.toml)

GitHub: [yangjiafengzi/kbimporter](https://github.com/yangjiafengzi/kbimporter)

把 Zotero 文献、项目文献、田野调查笔记统一整理为 Markdown，经双层切片写入 Milvus，
并提供增量导入、OCR 转换、去重清理与检索。向量化在 Milvus 服务端完成，本地无需 GPU
也能建库（OCR 环节是否要 GPU 另算）。

本项目是 [KB-Vectorize](https://github.com/yangjiafengzi/KB-Vectorize) 的工程化继任版本：
保留其“三库分构 + 双层切片 + Milvus 服务端向量化”的核心设计，补齐了可安装的 CLI、
环境引导、OCR 引擎链（Marker / MinerU / PaddleOCR 等）、Zotero 同步、去重与状态巡检。

## 适用场景

- 学术文献管理：Zotero 是唯一事实来源，PDF/EPUB 同步进文献库，转成 Markdown 后统一向量化。
- 项目文献专题库：每个项目独立建库，先人工筛选，再优先复用文献库中已转换的 Markdown，避免重复 OCR。
- 田野调查笔记：按项目组织访谈/观察笔记，自动提取 `_项目信息.md` 中的地点、时间、人员等元数据。
- RAG / Agent 检索：Milvus 稠密向量（DashScope 嵌入）+ 稀疏向量（BM25）双路召回。
- 中文社科研究：文件名、目录名、切片与检索全部面向中文场景设计。

## 数据库设计思想

### 目录结构

```text
知识库/
├── zotero文献库/
│   └── library/作者 - 年份 - 标题.md
├── 项目文献/
│   └── 项目名/作者 - 年份 - 标题.md
└── 田野调查笔记/
    └── 项目名/
        ├── _项目信息.md
        ├── 笔记/...
        └── 其他材料/...
```

PDF/EPUB 转换后，会在原目录生成同名 `.md`；导入只处理 `.md` 文件。

### 三库分构

不同来源的数据进入不同的 Milvus 集合，互不污染，也便于按来源/项目检索与清理：

| 来源 | 目录约定 | Milvus 集合 | 特有标量字段 |
| --- | --- | --- | --- |
| Zotero 文献库 | `<根>/zotero文献库/library` | `academic_library` | `language, author, year, title` |
| 项目文献 | `<根>/项目文献/<项目名>` | `proj_<项目名拼音>` | `project_name, language, author, year, title` |
| 田野调查笔记 | `<根>/田野调查笔记/<项目>/笔记 或 其他材料` | `fieldwork_kb` | `source_type, source_path, project_name, location, research_date, researchers, notes` |

所有集合共享通用字段：

```text
id, text, source_file, chunk_index, granularity,
parent_id, vector, sparse, created_at
```

- `source_file` 是增量更新与删除的唯一锚点，按“相对知识库根目录”保存。
- `granularity = coarse / fine` 区分两层切片；`parent_id` 把细块挂到所属粗块。

### 文件名即元数据

文献统一命名为 `作者 - 年份 - 标题.md`（作者与标题可含中文）。导入时自动从文件名解析
`author / year / title`，`language` 按文件名中中英文字符比例判断；不符合命名规范也能导入，
只是元数据为空。`_项目信息.md` 不切片，只被解析为田野笔记的元数据字段，改动后自动
`upsert` 到该项目的全部记录。

#### Zotero 侧配合：先命名，再同步

文件名规范的源头在 Zotero。请在 Zotero 中开启自动附件重命名（首选项 -> 高级 -> 文件与
文件夹，选择“作者 - 年份 - 标题”），新附件会自动按此规则命名；对已存在的附件，在每次
`kb sync-zotero` 前先手动完成一次命名（选中附件后右键 -> 重命名文件）。Zotero storage
里的文件名符合规范后，同步到文献库的文件名才会是 `作者 - 年份 - 标题.pdf/.epub`，转换出的
MD 才能解析出完整的 `author / year / title` 元数据。

### 双层切片

每份 Markdown 切成两层：

- 粗块（默认 8 KB，重叠 1 KB）：保留段落上下文，作为检索召回的主体；
- 细块（默认 1 KB，重叠 256 B）：提高精确命中率，细块的 `parent_id` 指向所属粗块，
  方便检索后回溯上下文。

切片按 UTF-8 字节长度控制，优先在 `\n\n`、`\n`、中文标点处断开。

### 向量化发生在 Milvus 服务端

集合创建时注册两个 Milvus Function：

- `text_dense_emb`：`provider=dashscope, model=text-embedding-v3`，生成稠密向量；
- `text_bm25_emb`：BM25 生成稀疏向量。

因此 **DashScope 密钥只需配置在 Milvus 服务端**（容器环境变量
`MILVUSAI_DASHSCOPE_API_KEY`，或 `deploy/user.yaml` 的 `credential`），运行 `kb` 的电脑
不需要 `DASHSCOPE_API_KEY`。`kb_config.toml` 中的 `embedding_provider / embedding_model /
embedding_dim` 决定建集合时的向量配置。

### 增量导入

SQLite 状态库（默认 `<根>/.kb/state.db`）记录每个文件的路径、hash、状态、所属集合与切片数。
每次 `kb import`：

| 情形 | 处理 |
| --- | --- |
| 新文件 | 切片并写入对应集合 |
| hash 变化 | 先删除该 `source_file` 的旧向量，再重写 |
| 内容未变 | 跳过 |
| 上次中断（processing） | 重新处理 |
| 文件被删除 | 按 `source_file` 精确清理 Milvus 向量并清状态记录 |
| 项目目录消失 | 只清理真正为空的 `proj_*` 集合，非空集合一律保留 |

### 安全默认值

- 所有删除/替换先移入 `<根>/.kb/trash` 回收目录，不直接删除；
- 破坏性命令默认 `--dry-run`，预演只读、不写状态库、不调用 Milvus；
- 云端 OCR 默认关闭，必须显式启用；
- API 密钥只从环境变量读取，禁止写入配置或代码；
- 项目文献中的同名 MD 替换默认 `ocr_only`：只用知识库内 `zotero文献库/library/` 中的
  MD（它们本身也多为 OCR 产物）顶替本程序记录为 OCR 产物的 MD，单向替换合理且安全；
  来源未知的现有 MD 默认不覆盖，人工整理的文件受保护。

## 安装

### 1. 准备 Milvus

需要 Milvus 2.6+（向量化依赖 Collection Function）。**已实测 Milvus 2.6.14；
Milvus 3.x 尚未实测**（pymilvus 已按 3.0 API 适配，但 Milvus 3.x 服务端需另行验证）。

官方 v2.6.x 容器不能只用一条 `docker run` 启动：镜像 `Cmd=null`，必须显式执行
`milvus run standalone`，并且要启用内嵌 etcd（默认配置会去连外部 etcd，导致反复
`connection refused`）。仓库已提供一键配置：

```text
deploy/
├── milvus-standalone.yml   # docker compose（已包含所有必需 env / CMD）
├── embedEtcd.yaml          # 内嵌 etcd 配置
└── user.yaml               # DashScope 自定义端点模板（默认全注释）
scripts/
├── start-milvus.ps1        # Windows
└── start-milvus.sh         # macOS / Linux
```

启动步骤：

```powershell
# 1. 设置 Milvus 侧密钥（注意变量名是 MILVUSAI_DASHSCOPE_API_KEY，不是 DASHSCOPE_API_KEY）
$env:MILVUSAI_DASHSCOPE_API_KEY = "sk-xxx"

# 2. 国内网络可先拉镜像并改名（可选）
docker pull daocloud.io/milvusdb/milvus:v2.6.14
docker tag daocloud.io/milvusdb/milvus:v2.6.14 milvusdb/milvus:v2.6.14

# 3. 启动
scripts\start-milvus.ps1
```

macOS / Linux 把第 1 步换成 `export MILVUSAI_DASHSCOPE_API_KEY=sk-xxx`，第 3 步换成
`./scripts/start-milvus.sh`。脚本内部执行的是与 `deploy/milvus-standalone.yml` 等价的
docker compose 命令；也可以直接：

```bash
docker compose -f deploy/milvus-standalone.yml up -d
```

与脚本等价的 `docker run`（排障/复现用）：

```powershell
docker run -d --name milvus --security-opt seccomp:unconfined `
  -e ETCD_USE_EMBED=true -e ETCD_DATA_DIR=/var/lib/milvus/etcd `
  -e ETCD_CONFIG_PATH=/milvus/configs/embedEtcd.yaml `
  -e COMMON_STORAGETYPE=local -e DEPLOY_MODE=STANDALONE `
  -e "MILVUSAI_DASHSCOPE_API_KEY=sk-xxx" `
  -v <你的目录>\volumes\milvus:/var/lib/milvus `
  -v <你的目录>\embedEtcd.yaml:/milvus/configs/embedEtcd.yaml `
  -v <你的目录>\user.yaml:/milvus/configs/user.yaml `
  -p 19530:19530 -p 9091:9091 -p 2379:2379 `
  milvusdb/milvus:v2.6.14 milvus run standalone
```

**DashScope 端点（重要）**

- 默认场景：只设置 `MILVUSAI_DASHSCOPE_API_KEY` 即可。
- 阿里云 MaaS 自定义端点：编辑 `deploy/user.yaml` 取消注释。Milvus 2.6 的 DashScope
  provider 走**原生接口**，`url` 必须是
  `https://<你的端点>/api/v1/services/embeddings/text-embedding/text-embedding`，
  不能填 OpenAI 兼容的 `compatible-mode/v1` 地址，否则报 `404 Not Found` 或
  `text:[1], embedding:[0]`。
- 这与 `kb` 本机云端 OCR 用的 `DASHSCOPE_API_KEY` + `[cloud_ocr.openai] base_url`
  （compatible-mode）是**两组不同的配置**，不要混淆。

配置好后可运行 `kb doctor --deep` 做端到端嵌入体检（临时集合，自动清理）。

如果已有 Milvus（例如旧 KB-Vectorize 部署），直接复用即可；程序只创建自己缺失的集合，
不会改动已有集合结构。

### 2. 安装 kbimporter

需要 Python 3.11+。

从发行包安装（按需选择 extras，避免一次性拉入 Marker/PyTorch 等重型依赖）：

```bash
# 核心：向量化导入 + Zotero 同步 + 去重（体积小，任何用法都需要）
pip install "kbimporter-0.5.0-py3-none-any.whl[import,sync,dedupe]"
# 之后按 OCR 方案补装：
pip install "kbimporter-0.5.0-py3-none-any.whl[ocr]"       # 本地引擎（Marker/MarkItDown，较重）
pip install "kbimporter-0.5.0-py3-none-any.whl[cloud]"     # 云端 OCR（PaddleOCR/MinerU/百度/OpenAI）
```

发行包本身不捆绑第三方依赖，extras 按需安装：`[import]`（向量化）、`[sync]`（Zotero
同步）、`[ocr]`（本地转换，含 marker-pdf 与 markitdown，会拉入 PyTorch/opencv/
onnxruntime，体积 GB 级；`[convert]` 是它的旧别名）、
`[cloud]`（云端 OCR 客户端）、`[dedupe]`（去重）、`[search]`（检索，与 `[import]`
共用 pymilvus）。`markitdown` 还会传递安装 `magika -> onnxruntime`，体积不小；只做
PDF 本地 OCR 时也可以只装 `marker-pdf`（`pip install marker-pdf`），不装 markitdown。
不想挑选时也可以用 `[all]` 一次装齐，但它包含 `[ocr]` 的全部重型依赖；若已发布到
PyPI，把 `[...]` 前的发行包名换成 `kbimporter` 即可，如
`pip install "kbimporter[import,sync,dedupe]"`。

**国内网络加速（PyPI / Docker Hub）**

PyPI 长时间无进展时切换阿里云镜像：

```bash
pip install -i https://mirrors.aliyun.com/pypi/simple/ "kbimporter[import,sync,dedupe]"
# 或设置全局默认源：
$env:PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"   # Windows
export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/      # macOS / Linux
```

Docker Hub 直连超时时，先拉镜像再改名，然后照常启动：

```bash
docker pull daocloud.io/milvusdb/milvus:v2.6.14
docker tag daocloud.io/milvusdb/milvus:v2.6.14 milvusdb/milvus:v2.6.14
# 或直接用 compose 的镜像变量（无需改名）：
$env:MILVUS_IMAGE = "daocloud.io/milvusdb/milvus:v2.6.14"   # Windows
export MILVUS_IMAGE=daocloud.io/milvusdb/milvus:v2.6.14      # macOS / Linux
docker compose -f deploy/milvus-standalone.yml up -d
```

从源码安装（推荐，含 `kb setup` 引导）：

```bash
git clone https://github.com/yangjiafengzi/kbimporter.git
cd 知识库导入程序
python -m venv .venv
# Windows（先装核心，再按 OCR 方案补装）
.venv\Scripts\pip install -e ".[import,sync,dedupe]"
# macOS / Linux
.venv/bin/pip install -e ".[import,sync,dedupe]"
```

`kb setup` 会先询问显卡与 OCR 方案，再按方案自动安装对应 extras：本地/混合方案加装
`[ocr]`，云端/混合方案加装 `[cloud]`，不再默认安装 `[all]`。

**全局命令（任意目录执行 `kb`）**

`kb` 安装后会把可执行文件放进虚拟环境的 `Scripts`（Windows）或 `bin`（macOS/Linux）：

- Windows：把 `.venv\Scripts` 加入 PATH；或直接把
  `.venv\Scripts\kb.exe` 复制到 `%APPDATA%\Microsoft\Windows\Start Menu\Programs` 之外的
  任意 PATH 目录。
- 配置查找顺序：`--config` > 环境变量 `KB_CONFIG` > 当前目录 `kb_config.toml` >
  项目根 > `%APPDATA%\kbimporter\kb_config.toml`（macOS/Linux 为
  `~/.config/kbimporter/kb_config.toml`）。
- 全局使用建议：`kb init --root D:\知识库 --output "$env:APPDATA\kbimporter\kb_config.toml"`
  后设置 `KB_CONFIG` 指向该文件，任何目录直接 `kb status` 都能读到。

**Windows GBK 控制台乱码**

`kb_config.toml` 和日志是 UTF-8；GBK 控制台看注释会乱码，先执行 `chcp 65001` 再查看。
程序本身在启动时会尝试把 stdout/stderr 切到 UTF-8，命令输出不受影响。

### 3. 安装外部 OCR 引擎（按需）

建议所有重型依赖（Marker、MarkItDown、PyMuPDF、PDFPlumber 等）都安装到虚拟环境中，
不要装进系统 Python，避免污染主环境、也方便整体卸载。Windows 下使用项目
`.venv\Scripts\pip install ...`，macOS/Linux 下使用 `.venv/bin/pip install ...`；
MinerU 依赖更复杂，建议单独建 conda 环境（见下）。

- Marker（文字版 PDF，CPU 可跑）：装 `[ocr]` extra 即可
  （`.venv\Scripts\pip install -e ".[ocr]"`，macOS/Linux 同理）；它体积较大
  （含 PyTorch），只用云端 OCR 或 MinerU 时可以不装
- MinerU（扫描件/公式，推荐 GPU）：
  `conda create -n mineru_env python=3.10 && conda activate mineru_env && pip install mineru`；
  国内网络可先设置 `MINERU_MODEL_SOURCE=modelscope`；它装在自己的 conda 环境中，
  程序通过 `[converter].mineru_cmd` 找到该环境的 `mineru` 命令即可
- MarkItDown（Word/PPT/Excel/EPUB 等非 PDF）：`[ocr]` 已包含；也可以单独安装
  （`.venv\Scripts\pip install markitdown`，macOS/Linux：`.venv/bin/pip install markitdown`）
- 云端 OCR：无需安装引擎，只需设置 API 密钥（见“如何选择本地/云端 OCR”）

### 4. 初始化并体检

```bash
kb init --root D:\知识库     # 生成 kb_config.toml 并创建目录结构
kb doctor                    # 体检：依赖/引擎/密钥/Milvus 可达性（只读）
kb setup                     # 或让程序引导创建虚拟环境、按需安装依赖、推荐 OCR 方案
```

`kb init` 会交互询问是否有高性能显卡并推荐 OCR 方案；`kb setup` 会先扫描本机环境，再
询问显卡与 OCR 方案，并按方案按需安装依赖，不再默认安装全部重型包。

## 使用

### 一次完整流程

```bash
kb status                      # 查看知识库与状态（只读）
kb sync-zotero --dry-run       # 预演前先在 Zotero 中完成附件重命名（见“Zotero 侧配合”）
kb sync-zotero                 # 实际同步（新增/替换，旧版本进回收目录）
kb convert --dry-run           # 预演转换，查看需要 OCR 的文件
kb convert                     # 文档 -> Markdown（同名 MD 已存在则跳过）
kb import --dry-run            # 预演向量化导入（只读）
kb import                      # 真正写状态库 + Milvus（首次执行前请确认）
kb search --collection academic_library --kind dense "你的问题"
```

### 1. 建立 Zotero 文献库（学术文献）

1. 在 Zotero 中给文献添加 PDF/EPUB 附件（直接拖入条目，或右键条目 -> 添加附件）。
2. 打开 Zotero 首选项 -> 高级 -> 文件与文件夹，勾选“自动将附件重命名为”，模板选择
   “作者 - 年份 - 标题”。新附件会自动命名；对已有附件，选中后右键 -> 重命名文件，
   手动完成一次命名（见上文“Zotero 侧配合”）。
3. 在项目目录先预演，确认要同步的文件：
   ```bash
   kb sync-zotero --dry-run
   ```
   确认无误后执行：
   ```bash
   kb sync-zotero
   ```
   程序会扫描 Zotero storage，按基础文件名分组，自动选择“中文比例最低”的版本（原文）
   复制到 `zotero文献库/library/`；旧版本和过期记录会移入回收目录。
4. 转换 PDF/EPUB 为 Markdown：
   ```bash
   kb convert --dry-run
   kb convert
   ```
   转换后在原目录生成同名 `.md`；已存在同名 `.md` 的文件自动跳过，避免重复 OCR。
5. 增量导入向量库：
   ```bash
   kb import --dry-run
   kb import
   ```
   文件进入 `academic_library` 集合，文件名自动解析出 `author / year / title`。
6. 以后新增文献时，只需对新文献重复第 2 步（命名）和第 3~5 步；程序是增量的，
   未变化的文件不会重复处理。

### 2. 建立项目文献库（Zotero 筛选 + 手动复制）

项目文献库不是全库同步，而是按项目人工筛选：

1. 在 Zotero 中按项目建一个收藏夹（或用搜索/标签），把该项目相关的文献拖进去。
2. 选中收藏夹中的条目，右键 -> “显示文件”（Show File），在文件管理器中找到对应
   PDF/EPUB；也可以直接把附件拖拽到目标文件夹（Zotero 会复制一份）。
3. 手动把选中的 PDF/EPUB 复制到 `知识库/项目文献/<项目名>/`。项目名用中文即可，
   Milvus 集合会自动生成 `proj_<项目名拼音>`；文件名保持 `作者 - 年份 - 标题.pdf`
   规范，便于解析元数据。
4. 转换并导入：
   ```bash
   kb convert --dry-run
   kb convert
   kb import --dry-run
   kb import
   ```
5. 避免重复 OCR：如果这些文献在文献库中已经转换过同名 MD，转换时会自动跳过
   （`skip_existing_md` 默认开启）。之后运行 `kb dedupe --dry-run` 预演、再
   `kb dedupe --execute`，可以把 `zotero文献库/library/` 中已有的同名 MD 复制/顶替到
   项目目录（默认只替换本程序记录的 OCR 产物，旧文件进回收目录）。

### 3. 建立田野调查数据库

田野调查按项目组织，每个项目一个文件夹：

```text
田野调查笔记/
└── 项目名/                 # 例如“某村基层治理”
    ├── _项目信息.md         # 元数据模板，不会被切片
    ├── 笔记/                # 访谈/观察记录，*.md
    └── 其他材料/            # PDF 等补充材料，可被 convert 转 MD
```

`_项目信息.md` 模板：

```markdown
# 项目信息

## 基本信息
- 调研地点：某省某市某县某镇某村
- 调研时间：2026年3月
- 调研人员：张三、李四
- 调研主题：基层治理与产业发展

## 备注
关于某村产业发展和基层治理的田野调查项目。
```

操作步骤：

1. 在 `田野调查笔记/` 下创建项目文件夹，按上面的结构放置文件；笔记写成 `.md`
   （建议文件名带日期，如 `2026-03-15 访谈记录.md`）。
2. 有扫描件/PDF 时先转换：
   ```bash
   kb convert --dry-run
   kb convert
   ```
3. 导入：
   ```bash
   kb import --dry-run
   kb import
   ```
   `笔记/` 进入 `fieldwork_kb`（`source_type=note`），`其他材料/` 进入
   `fieldwork_kb`（`source_type=supplement`）；`_项目信息.md` 中的地点、时间、人员、
   备注会自动附加到该项目的全部记录。
4. 之后修改 `_项目信息.md`，再跑 `kb import` 会自动 `upsert` 更新元数据，
   无需重新导入笔记。

### 4. 在 Cherry Studio 中配置 Agent

前提：`kb import` 已完成，Milvus 集合可检索（可先用
`kb search --collection academic_library --kind dense "测试"` 验证）。

1. 安装并打开 Cherry Studio，在“设置 -> 模型服务”里配置好要使用的模型
   （DeepSeek、Qwen、GPT 等均可）。
2. 配置 Milvus MCP（Windows 实测可用）：
   - 先启动 Milvus（`scripts\start-milvus.ps1`），MCP 进程连不上 Milvus 会立即退出，
     Cherry Studio 会报 `Connection closed (-32000)`。
   - **优先从官方仓库下载最新版**：
     [zilliztech/mcp-server-milvus](https://github.com/zilliztech/mcp-server-milvus)。
     按其 README 安装（例如 `pip install mcp-server-milvus` 或
     `uvx mcp-server-milvus`），并以官方文档中的启动参数为准（新版可能是
     `--milvus-uri`）。
   - 旧版兼容说明（`0.1.1.dev9` 实测）：该版本与 `mcp>=2` 不兼容
     （报 `ModuleNotFoundError: mcp.server.fastmcp`），且 CLI 参数是**下划线**
     `--milvus_uri`。仅当官方最新版不可用/不兼容时才需要锁版本安装：
     ```bash
     pip install "mcp-server-milvus==0.1.1.dev9" "mcp<2"
     ```
   - 进入 Cherry Studio“设置 -> MCP 服务器 -> 添加服务器”，命令填完整路径
     （下面以旧版为例，新版请按官方 README 调整参数名）：
     ```text
     C:\Users\<你>\miniconda3\envs\<环境>\Scripts\mcp-server-milvus.exe --milvus_uri http://localhost:19530
     ```
   - 无认证可留空；添加成功后工具列表会出现 `milvus_*` 工具。
3. 新建助手（Agent）：把 `agents/academic_advisor.md`（学术写作顾问）或
   `agents/fieldwork_analyst.md`（田野调查数据分析师）的全文粘贴到“系统提示词”中。
   这两个文件就是现成的 Agent 范例，来自 KB-Vectorize。
4. 对话时选择已启用该 MCP 的模型。Agent 会按提示词调用 Milvus MCP 的检索工具，
   访问 `academic_library` / `proj_*` / `fieldwork_kb` 集合。
5. 如果暂时没有可用的 Milvus MCP，也可以把 `kb search --collection <集合> --kind
   dense|bm25|query <词>` 的用法写进系统提示词，让 Agent 通过命令行完成同样的检索
   （见 `agents/README.md`）。

> Cherry Studio 的 MCP 安装方式随版本略有差异，以软件内“MCP 服务器”面板提示为准。
> `mcp-server-milvus` 持续迭代，建议始终从官方仓库获取最新版；如遇到兼容问题，
> 再参考上面“旧版兼容说明”的锁定方案。

### 常用命令

| 命令 | 说明 |
| --- | --- |
| `kb help` / `kb help import` | 查看帮助 |
| `kb init --root <路径>` | 生成配置与目录 |
| `kb status` | 知识库与增量状态（只读） |
| `kb doctor` | 环境体检（只读）；`--deep` 做端到端嵌入体检（临时集合，写操作） |
| `kb setup` | 安装引导 |
| `kb scan [--state-only|--milvus-only]` | 扫描状态库与 Milvus（只读） |
| `kb sync-zotero [--dry-run]` | 同步 Zotero 文献 |
| `kb convert [--dry-run] [--engine marker\|mineru\|cloud]` | 文档转 Markdown |
| `kb import [--dry-run]` | 增量向量化导入 |
| `kb dedupe [--execute] [--scope project\|library\|all]` | 去重/替换（默认预演） |
| `kb search --collection <集合> --kind dense\|bm25\|query <词> [--filter 表达式]` | 检索 |
| `kb ocr status / mode local\|hybrid [local\|cloud]\|cloud / enable / disable / keys` | OCR 模式、优先级与密钥管理 |

## 如何选择本地 / 云端 OCR

### 决策速查

| 情况 | 推荐 | 理由 |
| --- | --- | --- |
| 有 NVIDIA 显卡 | 本地 MinerU | 免费、离线、隐私好，扫描件/公式/版面效果好 |
| 无显卡，PDF 是文字版 | 本地 Marker | 足够快，CPU 也能跑，不花钱 |
| 无显卡，扫描件/中文/手写 | PaddleOCR 云 API | 中文识别质量好，整本 PDF 异步处理 |
| 云端想更稳，PaddleOCR 失败时自动切换 | PaddleOCR + MinerU 云端次选 | 主 provider 失败后自动用 MinerU 重试 |
| 有 API 预算、想换通用视觉模型 | OpenAI 兼容接口（DashScope qwen3-vl-plus） | 可自由换模型 |
| 离线且敏感 | 本地 MinerU（或本地 Ollama + OpenAI 兼容端点） | 数据不出机器 |

### 三种模式

```bash
kb ocr status                                  # 当前模式/引擎链/密钥
kb ocr mode local                              # marker -> mineru，不花钱
kb ocr mode hybrid local --provider paddle --fallback mineru   # 本地优先，失败后自动云端（推荐）
kb ocr mode hybrid cloud --provider paddle --fallback mineru   # 云端优先，失败后自动本地
kb ocr mode cloud     --provider paddle --fallback mineru      # 全部 PDF 走云端
kb ocr enable --provider paddle --fallback mineru              # 等价于 hybrid local
kb ocr disable                                 # 关闭云端
kb ocr keys                                    # 查看各 provider 密钥是否已设置
```

混合模式可选本地/云端优先：`hybrid local` 的引擎链是
`marker -> mineru -> cloud`（本地先试，失败才花钱走云端）；`hybrid cloud` 的引擎链是
`cloud -> marker -> mineru`（云端先试，云端失败/没启用时再用本地引擎兜底，适合本地
没有 GPU 但希望优先用云端高精度解析的场景）。`kb ocr mode hybrid` 不带优先级时默认
本地优先；`kb ocr status` 会显示当前是“云端优先”还是“本地优先”。

`paddle` 是默认云端方案：整份 PDF 提交给 PaddleOCR 云端异步任务（PaddleOCR-VL-1.6），
服务端分页识别后返回 Markdown；程序轮询进度、下载结果并做本地缓存，中断后不会重复提交
已完成的任务。超过 100 页的 PDF 会自动拆分为多个子任务（可在 `[cloud_ocr.paddle]`
的 `max_pages_per_task` 调整），全部识别完成后按页序合并成一份 Markdown，每个子任务
独立断点续传。`mineru` 是 MinerU 精准解析云 API：先申请上传链接、PUT 上传整份 PDF，
再轮询 `extract-results/batch/{batch_id}`，下载结果 zip 中的 `full.md`；同样支持超过
200 页自动拆分子任务（`[cloud_ocr.mineru].max_pages_per_task`）和断点续传。
`fallback_providers` 可配置回退链（例如 `["mineru"]`），主 provider 失败后会自动尝试
下一个，云端识别更稳。`baidu` 与 `openai` 是按页/按批请求，同样带断点续传，适合大文件
分批 OCR。

**429 熔断（当日额度用完自动切换通道）**：PaddleOCR 每个模型每日解析上限为 3000 页，
用完后 API 会返回 `429`。程序识别到 429 后**不再重试当前 provider**，立即切换到
`fallback_providers` 里的下一个通道（如 MinerU），避免在无效重试上浪费时间；MinerU 的
日额度错误（`-60018`）同样处理。503/504 等瞬时错误仍按退避重试。

**并发与超时**：超过单任务页数上限（PaddleOCR 100 页 / MinerU 200 页）的 PDF 会拆成
多个子任务，按 `[cloud_ocr.paddle].max_workers` / `[cloud_ocr.mineru].max_workers`
（默认 2）并发解析，结果仍按页序合并；任一子任务失败（尤其额度耗尽）会取消剩余波次。
轮询进度超过 `stall_timeout`（默认 900 秒）不增长时判定任务卡死，自动重新提交，
重试耗尽后再切换通道。

### 开启 MinerU 云端次选（推荐）

MinerU 精准解析云 API 作为云端 OCR 的次选：主 provider（默认 PaddleOCR）整体失败时，
自动切换到 MinerU 云端重试，降低“云端也识别不出来”的概率。启用步骤：

1. 到 [MinerU API 管理页](https://mineru.net/apiManage/token) 创建 API token；
2. 设置环境变量（PowerShell），然后**重新打开终端**：

   ```powershell
   setx MINERU_API_KEY "你的MinerU token"
   ```

3. 切换到带 MinerU 次选的混合模式：

   ```bash
   # 本地优先 + 云端（PaddleOCR -> MinerU）兜底，推荐
   kb ocr enable --provider paddle --fallback mineru

   # 或者云端优先：所有 PDF 先走云端，云端失败再本地
   kb ocr mode hybrid cloud --provider paddle --fallback mineru
   ```

4. 预演确认（不调用 API）：

   ```bash
   kb convert --dry-run
   ```

5. 确认无误后执行 `kb convert`。`kb ocr status` 可随时查看当前模式、引擎链和密钥状态。

也可以手动在 `kb_config.toml` 中配置（`kb init` 生成的模板已自带）：

```toml
[cloud_ocr]
enabled = true
provider = "paddle"
fallback_providers = ["mineru"]   # 主 provider 失败后依次尝试的备选

[cloud_ocr.mineru]
api_key_env = "MINERU_API_KEY"
model_version = "vlm"             # pipeline / vlm（推荐）/ MinerU-HTML
is_ocr = true                     # 扫描件启用 OCR；文字版 PDF 可设 false 省钱
max_pages_per_task = 200          # 超过 200 页自动拆分子任务
```

### 密钥（只设在本机环境变量，不写配置）

```powershell
# PowerShell
setx PADDLE_OCR_API_KEY "你的token"            # PaddleOCR 云 API（首选）
setx MINERU_API_KEY "sk-..."                   # MinerU 精准解析云 API（次选）
setx BAIDU_OCR_API_KEY "你的key"               # 百度智能云
setx BAIDU_OCR_SECRET_KEY "你的secret"
setx DASHSCOPE_API_KEY "sk-..."                # OpenAI 兼容云端 OCR
```

设置后**重新打开终端**，再运行 `kb doctor` 或 `kb ocr keys` 验证。

注意区分两个密钥的用途：Milvus 服务端的 `MILVUSAI_DASHSCOPE_API_KEY`（或
`deploy/user.yaml` 的 credential）负责向量化嵌入；本机环境变量中的 `DASHSCOPE_API_KEY`
只在选择 `openai` 云端 OCR 时才需要。

### 风险与预演

云端 OCR 会产生 API 费用，且文档图片会发送到第三方服务；程序默认关闭。切换后先预演：

```bash
kb convert --engine cloud --dry-run
```

预演会显示页数与预计请求数，不实际调用 API。确认无误后再 `kb convert`。

## 配置

所有路径、切片大小、批大小、超时、引擎顺序都集中在 `kb_config.toml`
（模板见 `kb_config.example.toml`）。常用改动：

- `[paths].kb_root`：知识库根目录（也可用环境变量 `KB_ROOT`）
- `[paths].state_db`：增量状态库；老版本用户可直接指向旧 `import_state.db` 继续增量
- `[converter].engines`：OCR 引擎回退链
- `[cloud_ocr]`：云端 OCR 开关、provider 与 `fallback_providers` 回退链
- `[cloud_ocr.paddle]` / `[cloud_ocr.mineru]`：云端 API 参数（模型版本、OCR 开关、
  页数上限、子任务并发数 `max_workers`、卡死判定 `stall_timeout` 等）
- `[milvus]`：Milvus 地址与 embedding 配置

完整配置参考与开发/测试说明见 [AGENTS.md](AGENTS.md)。

## 配套文档与示例

- [docs/DB_GUIDE.md](docs/DB_GUIDE.md)：数据库结构详细说明——集合 Schema、父子块设计、
  索引配置、增量状态表、检索命令与 filter 语法、写操作禁令。
- [agents/](agents/)：AI Agent 系统提示词示例（田野调查数据分析师、学术写作顾问），
  来自 KB-Vectorize，可直接复制到支持 MCP 的 AI 客户端使用。
- [AGENTS.md](AGENTS.md)：维护者与 AI Agent 的行为规则、完整配置参考、开发测试流程。

## License

MIT
