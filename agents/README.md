# Agent 提示词示例

本目录提供两个可直接使用的 AI Agent 系统提示词，来自
[KB-Vectorize](https://github.com/yangjiafengzi/KB-Vectorize)（MIT 许可），
配合本项目向量化后的 Milvus 集合使用。

| 文件 | Agent 角色 | 使用的集合 | 需要的 MCP 工具 |
| --- | --- | --- | --- |
| `fieldwork_analyst.md` | 田野调查数据分析师 | `fieldwork_kb` | Milvus MCP、Math MCP |
| `academic_advisor.md` | 学术写作顾问 | `academic_library`、`proj_*` | Milvus MCP、Web Search MCP |

## 需要的 MCP 服务

### Milvus MCP（两个 Agent 都需要）

- `milvus_list_collections` — 列出集合
- `milvus_load_collection` — 加载集合到内存
- `milvus_get_collection_info` — 查看集合 Schema
- `milvus_text_search` — 无过滤 BM25 关键词检索（不支持 `filter_expr`）
- `milvus_text_similarity_search` — 语义检索（`anns_field="vector", metric_type="IP"`）或带过滤 BM25（`anns_field="sparse", metric_type="BM25"`）
- `milvus_query` — 标量过滤查询（取父块、核实来源）

> ⚠️ 检索前若集合未加载，先调用 `milvus_load_collection`；禁用 `milvus_vector_search` / `milvus_hybrid_search`（需要外部嵌入工具，本环境没有）。

### Math MCP（仅田野调查数据分析师）

`calc`、`statistics`、`percentage`、`compoundInterest`、`proportion`。
所有数值计算必须通过工具完成，禁止 Agent 心算。

### Web Search MCP（仅学术写作顾问）

通用联网搜索工具，建议并行配置多个搜索引擎；每次检索必须中英文各跑一遍。

## 使用方式

1. 先用 `kb import` 把文档向量化到 Milvus（集合名与提示词中的一致）。
2. 在 AI 客户端中配置并连接上述 MCP 服务。
3. 把对应提示词文件的内容复制为系统提示词（Claude、GPT 等支持 MCP 的客户端均可）。
4. 通过 Agent 直接查询知识库。

> 提示：两个提示词是通用方法论模板。如果环境里没有 Milvus MCP，也可以让 Agent
> 改用本项目命令行 `kb search --collection <集合> --kind dense|bm25|query <词>`
> 完成同样的检索；集合名与字段名不变。

> 来源：[KB-Vectorize](https://github.com/yangjiafengzi/KB-Vectorize)
> （MIT License），原文未作删改，使用请保留出处。
