from __future__ import annotations

from kbimporter.config import Config


def _pm():
    """延迟导入 pymilvus，便于在不安装 Milvus 依赖的环境下测试其他模块。"""
    try:
        import pymilvus
    except ImportError:
        raise RuntimeError(
            "缺少依赖 pymilvus：请安装 kbimporter[import] 或 pip install pymilvus"
        )
    return pymilvus


def connect(cfg: Config):
    pm = _pm()
    pm.connections.connect(host=cfg.milvus.host, port=cfg.milvus.port)


def ensure_connected(cfg: Config):
    """确保已建立 Milvus 连接（幂等），避免 ORM API 报 ConnectionNotExist。"""
    pm = _pm()
    try:
        conn_mod = pm.connections
        if conn_mod.has_connection("default"):
            return
        conn_mod.connect(host=cfg.milvus.host, port=cfg.milvus.port)
    except AttributeError:
        # 环境未提供 ORM connections（测试替身 / 仅 MilvusClient），跳过
        return


def _base_fields(cfg: Config):
    pm = _pm()
    return [
        pm.FieldSchema(name="id", dtype=pm.DataType.INT64, is_primary=True, auto_id=True),
        pm.FieldSchema(
            name="text", dtype=pm.DataType.VARCHAR, max_length=65535,
            enable_analyzer=True, analyzer_params={"type": "chinese"},
        ),
        pm.FieldSchema(name="source_file", dtype=pm.DataType.VARCHAR, max_length=512),
        pm.FieldSchema(name="chunk_index", dtype=pm.DataType.INT32),
        pm.FieldSchema(name="granularity", dtype=pm.DataType.VARCHAR, max_length=16),
        pm.FieldSchema(name="parent_id", dtype=pm.DataType.INT64),
        pm.FieldSchema(name="vector", dtype=pm.DataType.FLOAT_VECTOR, dim=cfg.milvus.embedding_dim),
        pm.FieldSchema(name="sparse", dtype=pm.DataType.SPARSE_FLOAT_VECTOR),
        pm.FieldSchema(name="created_at", dtype=pm.DataType.INT64),
    ]


def _build_functions(cfg: Config):
    pm = _pm()
    dense_func = pm.Function(
        name="text_dense_emb",
        input_field_names=["text"],
        output_field_names=["vector"],
        function_type=pm.FunctionType.TEXTEMBEDDING,
        params={
            "provider": cfg.milvus.embedding_provider,
            "model_name": cfg.milvus.embedding_model,
        },
    )
    bm25_func = pm.Function(
        name="text_bm25_emb",
        input_field_names=["text"],
        output_field_names=["sparse"],
        function_type=pm.FunctionType.BM25,
    )
    return [dense_func, bm25_func]


def _create_indexes(coll, cfg: Config, scalar_fields: list[str] | None = None):
    pm = _pm()
    coll.create_index(
        field_name="vector",
        index_params={
            "index_type": "HNSW",
            "metric_type": "IP",
            "params": {"M": cfg.milvus.hnsw_m, "efConstruction": cfg.milvus.hnsw_ef_construction},
        },
    )
    coll.create_index(
        field_name="sparse",
        index_params={"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"},
    )
    if scalar_fields:
        for field in scalar_fields:
            coll.create_index(
                field_name=field,
                index_params={"index_type": "INVERTED"},
            )


def ensure_academic_library(cfg: Config):
    pm = _pm()
    ensure_connected(cfg)
    name = "academic_library"
    if pm.utility.has_collection(name):
        return pm.Collection(name=name)
    fields = _base_fields(cfg) + [
        pm.FieldSchema(name="language", dtype=pm.DataType.VARCHAR, max_length=16),
        pm.FieldSchema(name="author", dtype=pm.DataType.VARCHAR, max_length=256),
        pm.FieldSchema(name="year", dtype=pm.DataType.INT32),
        pm.FieldSchema(name="title", dtype=pm.DataType.VARCHAR, max_length=512),
    ]
    schema = pm.CollectionSchema(fields=fields, description="学术文献总库")
    for func in _build_functions(cfg):
        schema.add_function(func)
    coll = pm.Collection(name=name, schema=schema)
    _create_indexes(coll, cfg, scalar_fields=["language"])
    return coll


def ensure_project_collection(coll_name: str, cfg: Config):
    pm = _pm()
    ensure_connected(cfg)
    if pm.utility.has_collection(coll_name):
        return pm.Collection(name=coll_name)
    fields = _base_fields(cfg) + [
        pm.FieldSchema(name="project_name", dtype=pm.DataType.VARCHAR, max_length=256),
        pm.FieldSchema(name="language", dtype=pm.DataType.VARCHAR, max_length=16),
        pm.FieldSchema(name="author", dtype=pm.DataType.VARCHAR, max_length=256),
        pm.FieldSchema(name="year", dtype=pm.DataType.INT32),
        pm.FieldSchema(name="title", dtype=pm.DataType.VARCHAR, max_length=512),
    ]
    schema = pm.CollectionSchema(fields=fields, description=f"项目文献库: {coll_name}")
    for func in _build_functions(cfg):
        schema.add_function(func)
    coll = pm.Collection(name=coll_name, schema=schema)
    _create_indexes(coll, cfg, scalar_fields=["project_name"])
    return coll


def ensure_fieldwork_kb(cfg: Config):
    pm = _pm()
    ensure_connected(cfg)
    name = "fieldwork_kb"
    if pm.utility.has_collection(name):
        return pm.Collection(name=name)
    fields = _base_fields(cfg) + [
        pm.FieldSchema(name="source_type", dtype=pm.DataType.VARCHAR, max_length=32),
        pm.FieldSchema(name="source_path", dtype=pm.DataType.VARCHAR, max_length=1024),
        pm.FieldSchema(name="project_name", dtype=pm.DataType.VARCHAR, max_length=256),
        pm.FieldSchema(name="location", dtype=pm.DataType.VARCHAR, max_length=256),
        pm.FieldSchema(name="research_date", dtype=pm.DataType.VARCHAR, max_length=64),
        pm.FieldSchema(name="researchers", dtype=pm.DataType.VARCHAR, max_length=512),
        pm.FieldSchema(name="notes", dtype=pm.DataType.VARCHAR, max_length=2048),
    ]
    schema = pm.CollectionSchema(fields=fields, description="田野调查知识库")
    for func in _build_functions(cfg):
        schema.add_function(func)
    coll = pm.Collection(name=name, schema=schema)
    _create_indexes(coll, cfg, scalar_fields=["source_type", "project_name"])
    return coll
