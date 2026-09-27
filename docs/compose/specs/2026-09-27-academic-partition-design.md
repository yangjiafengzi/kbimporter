# academic_library 语言×年代分区设计

## [S1] 问题

`academic_library` 创建时未设分区，822,667 行全在 `_default`。检索必须整库 `load_collection`，内存压力大。需要分区动态加载，且**不得**触发无谓的 DashScope 重嵌入费用。

## [S2] 约束（已验证，Milvus 2.6.14）

- 集合 Function（`text_dense_emb` / `text_bm25_emb`）使 `vector`/`sparse` 成为 function output，**禁止客户端 insert**；重插 text 会重新调用 DashScope。
- `query` 可导出稠密 `vector`；**不能**导出 `sparse`（BM25 由 text 免费重算，无需搬运）。
- 已有集合**不能**追加 `partition_key_field`；可用手工 `create_partition` + `insert(partition_name=)` + `load_partitions`。
- `drop_collection_function` / `add_collection_function` 可用：临时摘掉 dense Function 后可写回已导出向量。
- `auto_id=true` 集合 **不能** 显式插入旧 `id`（服务端拒绝）；`upsert` 也不保留旧 id。必须重映射 `parent_id`。

## [S3] 方案概述

在**原** `academic_library` 上做手工分区 + 向量保真搬运（不删库、不重嵌、可回滚）。

分区名：`{lang}_{bucket}`，`lang∈{zh,en}`，`bucket∈{pre1980,1980s,1990s,2000s,2010s,2020s,unknown}`（`year<=0` → `unknown`）。共 14 个分区。

## [S4] 迁移命令 `kb repartition academic-library`

默认 `--dry-run`。实际执行步骤：

1. 流式导出全部行（含 `vector`、标量、`id`/`parent_id`）
2. `drop_collection_function("text_dense_emb")`（保留 BM25）
3. 按目标分区 `create_partition`；先插全部 coarse（`parent_id=0`），记录 `old_id→new_id`
4. 再插 fine，`parent_id` 按映射改写
5. 清空 `_default` 中旧数据
6. `add_collection_function` 恢复 `text_dense_emb`
7. 校验行数；失败则删除新建分区并重新挂上 dense Function

**嵌入调用次数：0。** 仅在恢复 Function 时服务端可能做一次模型校验。

## [S5] 导入路由

`process_academic` 写入时带 `partition_name=f"{lang}_{year_bucket(year)}"`。`proj_*` / `fieldwork_kb` 不变。

## [S6] 检索与内存

```bash
kb search --collection academic_library --kind dense "词" --year-from 2015 --year-to 2022
kb search --collection academic_library --kind dense "词" --partitions zh_2010s,en_2010s
kb release academic_library
kb release academic_library --partitions zh_2010s
```

- 未指定分区/年份：保持现状，`load_collection` 全量加载
- 指定了则只 `load_partitions`；`--release` 可卸载
- `--year-from/to` 映射为覆盖区间的所有 `lang_bucket` 分区

## [S7] 提示词与文档

更新 `agents/academic_advisor.md`、`agents/README.md`、`agents_nopush/*.md`、`docs/DB_GUIDE.md`、`AGENTS.md`：

- 分区表与命名规则
- 检索 academic_library **必须**带 `--year-from/to` 或 `--partitions`
- 禁止无脑 `load_collection` 整库加载
- 示例提示词 + 实际使用提示词中的检索步骤

## [S8] 测试

- `year_bucket` / `academic_partition_name` 边界（0、缺 language、中英）
- repartition dry-run 不写库；id 映射；失败回滚
- search 年份→分区映射
- 全部使用假 pymilvus，不连真实 Milvus

## [S9] 非目标

- 不改 `proj_*` / `fieldwork_kb`
- 不引入客户端嵌入
- 不删除旧状态库
- 不全量重建集合
