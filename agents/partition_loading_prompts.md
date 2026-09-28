# academic_library 分区动态加载提示词

可直接粘贴进 Agent 系统提示词，或作为检索规程使用。
分区名 = `{语言}_{年代}`：

| 语言 | 年代 |
| --- | --- |
| `zh` / `en` | `pre1980` / `1980s` / `1990s` / `2000s` / `2010s` / `2020s` / `2030s` / `2040s` / `2050s` / `unknown` |

共 20 个分区，例如 `zh_2010s`、`en_unknown`（`year<=0`）。
`year ≥ 2060` 记入 `2050s`。

---

## 示例提示词（可粘贴进系统提示词）

```markdown
### academic_library 分区动态加载（必须遵守）

`academic_library` 按「语言 × 年代」做了手工分区。检索时**只加载所需分区**，
禁止整库 `load_collection("academic_library")`（大数据量会占满内存）。

**分区命名**：`{zh|en}_{pre1980|1980s|1990s|2000s|2010s|2020s|2030s|2040s|2050s|unknown}`
例：`zh_2010s`、`en_2020s`、`zh_unknown`

**检索 academic_library 的标准做法**：

1. 先从问题推断时间范围（如「近十年」→ 2015–2024；「1990 年代」→ 1990–1999）。
2. 用 CLI 按年份区间加载/检索，结束后 `--release`：

   kb search --collection academic_library --kind dense "<关键词>" \
     --year-from <起始年> --year-to <结束年> [--lang zh|en] --release

   # BM25 关键词
   kb search --collection academic_library --kind bm25 "<关键词>" \
     --year-from <起始年> --year-to <结束年> --release

   # 直接点名分区
   kb search --collection academic_library --kind dense "<关键词>" \
     --partitions zh_2010s,zh_2020s --release

3. 需要回取父块/原文时，用 `milvus_query` 按 `source_file` / `parent_id` 查询，
   不要为了取父块而整库加载。
4. `proj_*` 仍按「一次只加载一个 Collection」执行；`fieldwork_kb` 按其自身规则。
5. 全部检索结束后释放：`kb release academic_library --partitions <...>`。

**年份 → 分区速查**（不指定语言时中英都要）：
- 2015–2024 → `zh_2010s,zh_2020s,en_2010s,en_2020s`
- 2000–2009 → `zh_2000s,en_2000s`
- 1990–1999 → `zh_1990s,en_1990s`
- 全时段 → 20 个分区（尽量少用；确需全时段时显式列出或分批加载）
```

---

## 实际使用提示词（Agent 执行时的具体命令）

### 按问题推断年代并检索

| 用户问题 | 实际命令 |
| --- | --- |
| 近十年村民自治研究 | `kb search --collection academic_library --kind dense "村民自治" --year-from 2015 --year-to 2024 --release` |
| 1990 年代中国农村政治 | `kb search --collection academic_library --kind dense "农村政治" --year-from 1990 --year-to 1999 --release` |
| 2000 年后英文文献 on governance | `kb search --collection academic_library --kind dense "governance" --year-from 2000 --year-to 2024 --lang en --release` |
| 关键词精确匹配（近五年中文） | `kb search --collection academic_library --kind bm25 "富人治村" --year-from 2020 --year-to 2024 --lang zh --release` |
| 已知只需 2010 年代 | `kb search --collection academic_library --kind dense "关键词" --partitions zh_2010s,en_2010s --release` |
| 标量过滤 + 分区 | `kb search --collection academic_library --kind query --filter "author == '徐勇'" --partitions zh_2010s,zh_2020s` |

### 释放内存

```bash
kb release academic_library --partitions zh_2010s,zh_2020s   # 只卸部分
kb release academic_library                                  # 卸载整库
```

### 回取父块（MCP，不整库加载）

```text
milvus_query(
  collection_name="academic_library",
  filter_expr="id == <parent_id>",
  output_fields=["text","source_file","chunk_index","title","author","year"],
  limit=1
)
```

### 三路检索的分区用法（学术顾问类 Agent）

```text
# 路 1：BM25（中英文双搜，限定 fine + 年份分区）
kb search --collection academic_library --kind bm25 "<中文关键词>" --year-from Y1 --year-to Y2 --filter "granularity == 'fine'" --release
kb search --collection academic_library --kind bm25 "<英文关键词>" --year-from Y1 --year-to Y2 --lang en --filter "granularity == 'fine'" --release

# 路 2：语义 dense
kb search --collection academic_library --kind dense "<中文问句>" --year-from Y1 --year-to Y2 --release

# 路 3：扩展/反驳视角，可放宽年代（如前推 5 年）再搜一轮
```

### 维护者：历史数据迁移到分区（不重嵌入、0 费用）

```bash
kb repartition academic-library           # dry-run，只读
kb repartition academic-library --execute # 真正搬迁
```
