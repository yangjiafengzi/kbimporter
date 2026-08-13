from __future__ import annotations

import socket

from kbimporter.config import Config

_client_cache: dict[tuple[str, str], object] = {}


def _pm():
    """延迟导入 pymilvus，便于在不安装 Milvus 依赖的环境下测试其他模块。"""
    try:
        import pymilvus
    except ImportError:
        raise RuntimeError(
            "缺少依赖 pymilvus：请安装 kbimporter[import] 或 pip install pymilvus"
        )
    return pymilvus


def _client_uri(cfg: Config) -> str:
    return f"http://{cfg.milvus.host}:{cfg.milvus.port}"


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """快速 TCP 可达性检查（pymilvus 3.x 的 MilvusClient 是启动即连接）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tcp_reachable(cfg: Config, timeout: float = 2.0) -> bool:
    return tcp_reachable(cfg.milvus.host, int(cfg.milvus.port), timeout)


def get_client(cfg: Config):
    """获取（并缓存）MilvusClient 实例；pymilvus 3.x 推荐 API。"""
    pm = _pm()
    key = (cfg.milvus.host, cfg.milvus.port)
    if key not in _client_cache:
        if not _tcp_reachable(cfg):
            raise RuntimeError(
                f"无法连接 Milvus（{cfg.milvus.host}:{cfg.milvus.port}）。"
                "请先启动 Milvus 服务（如 Docker Desktop 中的 milvus 容器），"
                "或检查 [milvus] host/port 配置；可运行 `kb doctor` 查看详情。"
            )
        try:
            client = pm.MilvusClient(uri=_client_uri(cfg))
        except Exception as e:
            raise RuntimeError(
                f"连接 Milvus 失败（{_client_uri(cfg)}）：{e}\n"
                "请确认 Milvus 已启动且 host/port 配置正确；"
                "可运行 `kb doctor` 查看详情。"
            ) from e
        _client_cache[key] = client
    return _client_cache[key]


def connect(cfg: Config):
    """兼容入口：建立（或复用）MilvusClient 连接。"""
    return get_client(cfg)


def ensure_connected(cfg: Config):
    """确保存在可用的 MilvusClient（幂等）。"""
    return get_client(cfg)


class MilvusCollection:
    """内部集合句柄：把 MilvusClient 调用封装成少量 ORM 风格方法。"""

    def __init__(self, client, name: str):
        self._client = client
        self.name = name

    def load(self):
        self._client.load_collection(collection_name=self.name)

    def insert(self, rows: list[dict]) -> list[int]:
        result = self._client.insert(collection_name=self.name, data=rows)
        ids = result.get("ids", []) if isinstance(result, dict) else []
        return [int(i) for i in ids]

    def delete(self, expr: str):
        self._client.delete(collection_name=self.name, filter=expr)

    def upsert(self, rows: list[dict], partial_update: bool = False):
        kwargs = {"partial_update": True} if partial_update else {}
        self._client.upsert(collection_name=self.name, data=rows, **kwargs)

    def query(self, expr: str = "", output_fields: list[str] | None = None,
              limit: int | None = None, **kwargs):
        return self._client.query(
            collection_name=self.name,
            filter=expr,
            output_fields=output_fields,
            limit=limit,
            **kwargs,
        )

    def flush(self):
        self._client.flush(collection_name=self.name)

    @property
    def num_entities(self) -> int:
        stats = self._client.get_collection_stats(collection_name=self.name) or {}
        return int(stats.get("row_count", 0) or 0)


def _add_base_fields(schema, cfg: Config):
    pm = _pm()
    schema.add_field(
        field_name="id", datatype=pm.DataType.INT64, is_primary=True, auto_id=True
    )
    schema.add_field(
        field_name="text", datatype=pm.DataType.VARCHAR, max_length=65535,
        enable_analyzer=True, analyzer_params={"type": "chinese"},
    )
    schema.add_field(field_name="source_file", datatype=pm.DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="chunk_index", datatype=pm.DataType.INT32)
    schema.add_field(field_name="granularity", datatype=pm.DataType.VARCHAR, max_length=16)
    schema.add_field(field_name="parent_id", datatype=pm.DataType.INT64)
    schema.add_field(
        field_name="vector", datatype=pm.DataType.FLOAT_VECTOR,
        dim=cfg.milvus.embedding_dim,
    )
    schema.add_field(field_name="sparse", datatype=pm.DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(field_name="created_at", datatype=pm.DataType.INT64)


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


def _create_indexes(client, coll_name: str, cfg: Config,
                    scalar_fields: list[str] | None = None):
    params = client.prepare_index_params()
    params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="IP",
        params={"M": cfg.milvus.hnsw_m, "efConstruction": cfg.milvus.hnsw_ef_construction},
    )
    params.add_index(
        field_name="sparse",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
    )
    if scalar_fields:
        for field in scalar_fields:
            params.add_index(field_name=field, index_type="INVERTED")
    client.create_index(collection_name=coll_name, index_params=params)


def ensure_academic_library(cfg: Config):
    pm = _pm()
    client = ensure_connected(cfg)
    name = "academic_library"
    if client.has_collection(collection_name=name):
        return MilvusCollection(client, name)
    schema = pm.MilvusClient.create_schema(auto_id=True, description="学术文献总库")
    _add_base_fields(schema, cfg)
    schema.add_field(field_name="language", datatype=pm.DataType.VARCHAR, max_length=16)
    schema.add_field(field_name="author", datatype=pm.DataType.VARCHAR, max_length=256)
    schema.add_field(field_name="year", datatype=pm.DataType.INT32)
    schema.add_field(field_name="title", datatype=pm.DataType.VARCHAR, max_length=512)
    for func in _build_functions(cfg):
        schema.add_function(func)
    client.create_collection(collection_name=name, schema=schema)
    _create_indexes(client, name, cfg, scalar_fields=["language"])
    return MilvusCollection(client, name)


def ensure_project_collection(coll_name: str, cfg: Config):
    pm = _pm()
    client = ensure_connected(cfg)
    if client.has_collection(collection_name=coll_name):
        return MilvusCollection(client, coll_name)
    schema = pm.MilvusClient.create_schema(auto_id=True, description=f"项目文献库: {coll_name}")
    _add_base_fields(schema, cfg)
    schema.add_field(field_name="project_name", datatype=pm.DataType.VARCHAR, max_length=256)
    schema.add_field(field_name="language", datatype=pm.DataType.VARCHAR, max_length=16)
    schema.add_field(field_name="author", datatype=pm.DataType.VARCHAR, max_length=256)
    schema.add_field(field_name="year", datatype=pm.DataType.INT32)
    schema.add_field(field_name="title", datatype=pm.DataType.VARCHAR, max_length=512)
    for func in _build_functions(cfg):
        schema.add_function(func)
    client.create_collection(collection_name=coll_name, schema=schema)
    _create_indexes(client, coll_name, cfg, scalar_fields=["project_name"])
    return MilvusCollection(client, coll_name)


def ensure_fieldwork_kb(cfg: Config):
    pm = _pm()
    client = ensure_connected(cfg)
    name = "fieldwork_kb"
    if client.has_collection(collection_name=name):
        return MilvusCollection(client, name)
    schema = pm.MilvusClient.create_schema(auto_id=True, description="田野调查知识库")
    _add_base_fields(schema, cfg)
    schema.add_field(field_name="source_type", datatype=pm.DataType.VARCHAR, max_length=32)
    schema.add_field(field_name="source_path", datatype=pm.DataType.VARCHAR, max_length=1024)
    schema.add_field(field_name="project_name", datatype=pm.DataType.VARCHAR, max_length=256)
    schema.add_field(field_name="location", datatype=pm.DataType.VARCHAR, max_length=256)
    schema.add_field(field_name="research_date", datatype=pm.DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="researchers", datatype=pm.DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="notes", datatype=pm.DataType.VARCHAR, max_length=2048)
    for func in _build_functions(cfg):
        schema.add_function(func)
    client.create_collection(collection_name=name, schema=schema)
    _create_indexes(client, name, cfg, scalar_fields=["source_type", "project_name"])
    return MilvusCollection(client, name)
