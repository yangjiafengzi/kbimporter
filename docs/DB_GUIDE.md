# 向量数据库设计与使用指南

本文是 kbimporter 的数据库结构详细说明，面向需要检索、调试或二次开发的用户与 AI Agent。
综合整理自本知识库旧版 `0向量化/DB_GUIDE.md` 与
[KB-Vectorize](https://github.com/yangjiafengzi/KB-Vectorize) 的公开文档，
并已适配新程序的命令、状态库结构与安全规则。

## 一、整体设计

```text
Markdown 文件（三种来源）
    │
    ▼
scanner ──── 分类（academic / project / fieldwork）
    │          同时从文件名解析 author / year / title
    ▼
chunker ──── 双层切片：粗块(coarse) → 细块(fine)
    │
    ▼
importer ─── 批量写入 Milvus（向量由 Milvus 服务端自动生成）
    │
    ▼
Milvus ───── 稠密向量(text-embedding-v3) + 稀疏向量(BM25)
```

程序侧入口为 `kb import`：扫描 → 对比 hash → 切片 → 写入 Milvus，全程增量。

## 二、三库分构

数据按来源分为三个独立的 Collection，各有专属元数据字段：

| Collection | 来源 | 用途 |
| --- | --- | --- |
| `academic_library` | `zotero文献库/library/` | Zotero 导出的学术论文 |
| `proj_<拼音>` | `项目文献/<项目名>/` | 按研究项目分集合，动态创建 |
| `fieldwork_kb` | `田野调查笔记/` | 田野笔记、补充材料 |

**为什么要分离？** 学术文献有 `language` 字段，项目文献有 `project_name` 字段，
田野调查有 `location / researchers` 等字段——元数据结构不同，分开存更清晰，
检索时也更容易按来源限定范围。

## 三、项目集合命名

项目目录的中文名转拼音作为集合名：`村干部类型` → `proj_cunganbuleixing`。

集合在首次导入该项目文件时由程序自动创建。**空集合保护**：只有状态库中无记录且
`row_count == 0` 的 `proj_*` 集合才会被清理；状态库无记录但集合非空时一律保留，
防止误删有数据集合。

## 四、父子块（Coarse-Fine）设计

这是本系统的核心设计，解决的是**检索粒度与上下文的矛盾**。

### 4.1 问题

- 切片太小（如 500 字）：检索命中精确，但缺乏上下文，展示时支离破碎；
- 切片太大（如 8000 字）：上下文充足，但语义模糊，检索命中率低。

### 4.2 方案：双层切片

每个文档被切成两层：

```text
原始文档
  │
  ├── 粗块 (coarse) ── 8KB，重叠 1KB ── 保留上下文，用于展示
  │     ├── 细块 (fine) ── 1KB，重叠 256B ── 精确语义，用于检索
  │     ├── 细块 (fine)
  │     └── 细块 (fine)
  ├── 粗块 (coarse)
  │     ├── 细块 (fine)
  │     └── 细块 (fine)
  └── ...
```

### 4.3 字段关联

| 字段 | coarse 块 | fine 块 |
| --- | --- | --- |
| `granularity` | `"coarse"` | `"fine"` |
| `parent_id` | `0`（无父级） | 所属 coarse 块的 `id` |
| `chunk_index` | 在 coarse 序列中的序号 | 在 fine 序列中的序号 |

### 4.4 切片参数

| 参数 | coarse | fine |
| --- | --- | --- |
| 最大大小 | 8192 字节 | 1024 字节 |
| 重叠大小 | 1024 字节 | 256 字节 |

### 4.5 断点优先级

切片时提供以下候选分隔符：

```text
"\n\n" > "\n" > "。" > "！" > "？" > "；" > "，" > " "
```

实现细节：从文本约 60% 字节位置起向后搜索，在所有候选分隔符中取**位置最靠后**的
断点（而不是严格按上述顺序挑选）；找不到合适断点则硬切。

### 4.6 检索策略

1. **检索时**搜 fine 块（语义精确）；
2. **展示时**取 `parent_id` 对应的 coarse 块（上下文完整）；
3. 需要更大上下文时，可按 `chunk_index` 连续性取相邻粗块。

## 五、公共字段（所有集合）

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INT64 (PK, auto) | 主键，自动递增 |
| `text` | VARCHAR(65535) | 切片文本，中文分词分析器 |
| `source_file` | VARCHAR(512) | 来源文件相对路径 |
| `chunk_index` | INT32 | 同粒度内的序号 |
| `granularity` | VARCHAR(16) | `"coarse"` 或 `"fine"` |
| `parent_id` | INT64 | fine → coarse 的 id；coarse 为 0 |
| `vector` | FLOAT_VECTOR(1024) | 稠密向量（Milvus 自动生成） |
| `sparse` | SPARSE_FLOAT_VECTOR | 稀疏向量 BM25（Milvus 自动生成） |
| `created_at` | INT64 | Unix 时间戳 |

## 六、各集合专属字段

### 6.1 `academic_library`

| 字段 | 类型 | 来源 |
| --- | --- | --- |
| `language` | VARCHAR(16) | 文件名中中文/英文字符比例判断 |
| `author` | VARCHAR(256) | 文件名 `作者 - 年份 - 标题.md` 解析 |
| `year` | INT32 | 同上 |
| `title` | VARCHAR(512) | 同上 |

### 6.2 `proj_<拼音>`

| 字段 | 类型 | 来源 |
| --- | --- | --- |
| `project_name` | VARCHAR(256) | 项目目录中文名 |
| `language` | VARCHAR(16) | 文件名中中文/英文字符比例判断 |
| `author` | VARCHAR(256) | 文件名解析 |
| `year` | INT32 | 文件名解析 |
| `title` | VARCHAR(512) | 文件名解析 |

### 6.3 `fieldwork_kb`

| 字段 | 类型 | 来源 |
| --- | --- | --- |
| `source_type` | VARCHAR(32) | `"note"` 或 `"supplement"` |
| `source_path` | VARCHAR(1024) | 文件所在目录 |
| `project_name` | VARCHAR(256) | 项目名 |
| `location` | VARCHAR(256) | `_项目信息.md` 中读取 |
| `research_date` | VARCHAR(64) | 同上 |
| `researchers` | VARCHAR(512) | 同上 |
| `notes` | VARCHAR(2048) | 同上 |

## 七、Embedding 机制

向量**不由 Python 代码生成**，由 Milvus 内置 Function 在插入时自动处理：

- **稠密向量**：`text-embedding-v3`（DashScope），1024 维，基于 `text` 字段；
- **稀疏向量**：Milvus 内置 BM25 分词器，基于 `text` 字段。

**影响**：每次 insert 都会在 Milvus 服务端调用 DashScope API，产生费用。
重建 = 全量重新 embed = 全额费用。密钥只需配置在 Milvus 服务端，
运行 `kb` 的电脑不需要 `DASHSCOPE_API_KEY`。

## 八、索引设计

| 字段 | 索引类型 | 度量 | 说明 |
| --- | --- | --- | --- |
| `vector` | HNSW (M=16, efConstruction=256) | IP（内积） | 稠密向量近似搜索 |
| `sparse` | SPARSE_INVERTED_INDEX | BM25 | 稀疏向量全文搜索 |
| 标量字段 | INVERTED | — | 精确过滤；实际只为以下字段建索引 |

标量 INVERTED 索引的实际覆盖范围：

| 集合 | 已建索引的标量字段 |
| --- | --- |
| `academic_library` | `language` |
| `proj_*` | `project_name` |
| `fieldwork_kb` | `source_type`、`project_name` |

`author / year / title` 等字段没有标量索引：过滤仍可用（Milvus 会全量扫描），
但数据量大时性能不如索引字段。

## 九、增量状态管理

SQLite 状态库（默认 `<根>/.kb/state.db`）记录导入状态：

```text
file_state:
  file_path (PK) ── 文件路径
  file_hash ──────── SHA256，检测文件变更
  status ─────────── done / processing / deleted
  collection_name ── 所属 Milvus 集合
  chunk_count ────── 切片数量

project_meta_state:
  project_dir (PK) ─ 田野项目目录
  meta_hash ──────── `_项目信息.md` 的 hash，检测元数据变更

file_origin:
  file_path (PK) ── MD 文件路径
  origin ────────── zotero_md / ocr_md（用于替换策略）
  file_hash ─────── 写入时的文件 hash
```

每次 `kb import` 的行为：

| 场景 | 行为 |
| --- | --- |
| 新文件 | 切片 + 入库 |
| 文件已修改（hash 变化） | 删除旧向量 → 重新处理 |
| 文件未变化 | 跳过 |
| 上次中断（status=`processing`） | 重新处理 |
| 文件已删除 | 从 Milvus 和 SQLite 中清理 |
| 田野项目 `_项目信息.md` 变化 | upsert 更新该项目的元数据字段 |
| 项目 Collection 为空 | 仅删除真正为空的 `proj_*` 集合 |

老版本用户可把 `[paths].state_db` 直接指向旧 `import_state.db` 继续增量；
程序不会迁移旧库，缺 `file_origin` 表时自动按“未知来源”处理。

## 十、检索方式

### 10.1 命令行（推荐日常使用）

```bash
# 稠密向量语义检索
kb search --collection academic_library --kind dense "村干部的角色类型"

# BM25 关键词检索
kb search --collection fieldwork_kb --kind bm25 "富人治村"

# 条件查询（不走向量）
kb search --collection proj_cunganbuleixing --kind query \
  --filter "author == '李祖佩' and year >= 2020"

# 带过滤的语义检索
kb search --collection academic_library --kind dense "村干部角色" \
  --filter "year >= 2020"
```

### 10.2 Python 混合检索（dense + sparse，RRF 重排序）

```python
from pymilvus import Collection, AnnSearchRequest, RRFRanker

coll = Collection("academic_library")
coll.load()

dense_req = AnnSearchRequest(
    data=["社会学理论"], anns_field="vector",
    param={"metric_type": "IP", "params": {"ef": 128}}, limit=10,
)
sparse_req = AnnSearchRequest(
    data=["社会学理论"], anns_field="sparse",
    param={"metric_type": "BM25"}, limit=10,
)
results = coll.hybrid_search(
    reqs=[dense_req, sparse_req],
    rerank=RRFRanker(),
    limit=5,
    output_fields=["title", "author", "source_file"],
)
```

### 10.3 filter_expr 语法

```text
author == '李祖佩'                          # 字符串相等
author == '李祖佩' and year == 2021         # 多条件
year >= 2020 and year <= 2025              # 范围
source_file like '%村干部%'                  # 包含
author == '李祖佩' or author == '贺雪峰'     # 或
```

## 十一、写操作禁令

| 操作 | 风险 | 说明 |
| --- | --- | --- |
| `utility.drop_collection()` | **不可逆** | 删除全部向量数据，恢复需全量 re-embed（花钱+耗时） |
| 清空/重置 SQLite 状态库 | 危险 | 增量逻辑失效，可能产生重复数据 |
| 删除 Milvus 集合后重建 | 危险 | 全量重建 = 全额 embedding 费用 |
| `kb import`（非 dry-run） | 需确认 | 仅处理新增/修改文件，费用可控，但会写状态库与 Milvus |
| `kb dedupe --execute` | 需确认 | 默认 dry-run；执行时删除/替换先进回收目录 |
| `kb convert`（非 dry-run） | 需确认 | 会移动原文件、调用 OCR（可能产生费用） |

**规则：任何真实写入操作必须先向用户说明影响（费用、时间、数据量），获得明确许可。**
删除/替换必须先移入 `<根>/.kb/trash` 回收目录，不直接删除。
