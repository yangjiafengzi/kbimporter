# Agent 提示词示例

本目录提供两个可直接使用的 AI Agent 系统提示词，来自
[KB-Vectorize](https://github.com/yangjiafengzi/KB-Vectorize)（MIT 许可），
配合本项目向量化后的 Milvus 集合使用。

| 文件 | Agent 角色 | 使用的集合 | 需要的 MCP 工具 |
| --- | --- | --- | --- |
| `fieldwork_analyst.md` | 田野调查数据分析师 | `fieldwork_kb` | Milvus MCP、Math MCP |
| `academic_advisor.md` | 学术写作顾问 | `academic_library`、`proj_*` | Milvus MCP、Web Search MCP、Sciverse MCP（推荐） |

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

### Sciverse MCP（仅学术写作顾问，推荐启用）

在线学术文献检索，提供可引用（citation-grade）的论文级证据：

- `list_catalog` — 查看字段/枚举/过滤操作符（字段不确定时先调用）
- `search_papers` — 结构化元数据检索（标题/作者/年份/期刊/学科/引用量）
- `semantic_search` — 自然语言语义检索（RAG 切片，支持 `filters` 字段级约束）
- `read_content` — 按 `doc_id` + `offset` 扩读原文，核实观点
- `list_paper_relations` — 引用/被引/相关工作列表
- `get_resource` — 获取论文图表资源

规则：仅用于学术文献检索，不做通用网络/新闻搜索；每条 Sciverse 引用必须附 `doc_id` + title；工具不可用时跳过并注明，不得用模型记忆代替。客户端可能把工具暴露为 `sciverse_search_papers` 等带前缀名称，以实际可用名称为准。

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
