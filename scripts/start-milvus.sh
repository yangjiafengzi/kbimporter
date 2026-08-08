#!/usr/bin/env bash
# 启动 Milvus 2.6.14 单机容器（macOS / Linux）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/../deploy" && pwd)"

mkdir -p "$DEPLOY_DIR/volumes/milvus"

if [ -z "${MILVUSAI_DASHSCOPE_API_KEY:-}" ]; then
  echo "警告: 未设置 MILVUSAI_DASHSCOPE_API_KEY：容器能启动，但嵌入 Function 会失败。" >&2
  echo "设置方法: export MILVUSAI_DASHSCOPE_API_KEY='sk-xxx'" >&2
fi

if [ -n "${MILVUS_IMAGE:-}" ]; then
  echo "使用镜像: $MILVUS_IMAGE"
fi

docker compose -f "$DEPLOY_DIR/milvus-standalone.yml" up -d
echo "Milvus 已启动。验证: docker ps | grep milvus"
echo "嵌入体检: kb doctor --deep"
