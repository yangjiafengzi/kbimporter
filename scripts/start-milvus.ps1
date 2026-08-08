# 启动 Milvus 2.6.14 单机容器（Windows / Docker Desktop）
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$DeployDir = Join-Path $RepoRoot "deploy"
$VolumesDir = Join-Path $DeployDir "volumes\milvus"

New-Item -ItemType Directory -Force -Path $VolumesDir | Out-Null

if (-not $env:MILVUSAI_DASHSCOPE_API_KEY) {
    Write-Warning "未设置 MILVUSAI_DASHSCOPE_API_KEY：容器能启动，但嵌入 Function 会失败。"
    Write-Host "设置方法: `$env:MILVUSAI_DASHSCOPE_API_KEY='sk-xxx'"
}

if ($env:MILVUS_IMAGE) {
    Write-Host "使用镜像: $env:MILVUS_IMAGE"
}

docker compose -f (Join-Path $DeployDir "milvus-standalone.yml") up -d
Write-Host "Milvus 已启动。验证: docker ps | Select-String milvus"
Write-Host "嵌入体检: kb doctor --deep"
