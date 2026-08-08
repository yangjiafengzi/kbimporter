from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from kbimporter.config import Config, DEFAULT_CONFIG_FILE
from kbimporter.config_edit import set_toml_value as _set_config_value


# 核心功能（向量化导入 / Zotero 同步 / 去重）体积较小，任何 OCR 方案都需要；
# 重型依赖只出现在 convert（Marker/MarkItDown，含 PyTorch）与 cloud（云端 OCR API 客户端）中。
CORE_EXTRAS = ("import", "sync", "dedupe")
LOCAL_OCR_EXTRAS = ("ocr",)
CLOUD_OCR_EXTRAS = ("cloud",)


def _project_root() -> Path:
    """定位项目根目录（含 pyproject.toml），优先包所在源码目录。"""
    pkg_dir = Path(__file__).resolve().parent
    candidates = [pkg_dir.parent.parent, Path.cwd()]
    for cand in candidates:
        if (cand / "pyproject.toml").exists():
            return cand
    return Path.cwd()


def venv_status(venv_dir: Path) -> str:
    """检查虚拟环境状态：missing / exists_without_kb / exists_with_kb。"""
    if not venv_dir.exists():
        return "missing"
    if (venv_dir / "Scripts" / "kb.exe").exists() or (venv_dir / "bin" / "kb").exists():
        return "exists_with_kb"
    return "exists_without_kb"


def _pip_hint(venv_dir: Path, extras: list[str] | None = None) -> str:
    """按平台返回虚拟环境内 pip 的安装命令提示。"""
    spec = ".[all]" if not extras else f".[{','.join(extras)}]"
    if os.name == "nt":
        return f"{venv_dir.name}\\Scripts\\pip install -e {spec}"
    return f"{venv_dir.name}/bin/pip install -e {spec}"


def _ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y 或 n")


def _ask_gpu() -> bool:
    return _ask_yes_no("这台电脑有高性能显卡（NVIDIA GPU）吗？（用于决定 OCR 方案）", default=False)


def _ask_ocr_scheme() -> str:
    print("请选择 OCR 方案：")
    print("  1) 本地引擎优先（Marker/MinerU，不花钱）")
    print("  2) 云端 OCR（PaddleOCR 云 API，按量计费，无显卡推荐）")
    print("  3) 混合：本地失败后自动用云端")
    while True:
        choice = input("请输入 1、2 或 3（回车默认 1）：").strip() or "1"
        if choice in ("1", "2", "3"):
            return choice
        print("请输入 1、2 或 3")


def scheme_extras(scheme: str) -> list[str]:
    """按 OCR 方案返回需要安装的 extras。

    本地引擎（1）安装 Marker/MarkItDown；云端 OCR（2）安装云端客户端；
    混合（3）两者都装；核心依赖（import/sync/dedupe）始终安装。
    """
    extras = list(CORE_EXTRAS)
    if scheme in ("1", "3"):
        extras.extend(LOCAL_OCR_EXTRAS)
    if scheme in ("2", "3"):
        extras.extend(CLOUD_OCR_EXTRAS)
    return extras


def create_venv(venv_dir: Path, logger: logging.Logger) -> bool:
    if venv_dir.exists():
        logger.info(f"虚拟环境已存在，直接复用: {venv_dir}")
        return True
    logger.info(f"创建虚拟环境: {venv_dir}")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
        )
        return True
    except Exception as e:
        logger.error(f"创建虚拟环境失败: {e}")
        return False


def install_into_venv(venv_dir: Path, extras: list[str], project_root: Path,
                      logger: logging.Logger) -> bool:
    py = venv_dir / "Scripts" / "python.exe"
    if not py.exists():
        py = venv_dir / "bin" / "python"
    logger.info("先安装 setuptools...")
    try:
        subprocess.run(
            [str(py), "-m", "pip", "install", "setuptools"],
            check=True,
        )
    except Exception as e:
        logger.error(f"安装 setuptools 失败: {e}")
        return False
    spec = "." if not extras else f".[{','.join(extras)}]"
    logger.info(f"在 {project_root} 安装依赖: pip install -e {spec}")
    try:
        subprocess.run(
            [str(py), "-m", "pip", "install", "-e", spec],
            cwd=str(project_root),
            check=True,
        )
        return True
    except Exception as e:
        logger.error(f"安装依赖失败: {e}")
        logger.info("如因网络超时/长时间无进展，请切换国内 PyPI 镜像后重试，例如：")
        logger.info("  pip install -i https://mirrors.aliyun.com/pypi/simple/ "
                    f"-e {spec}")
        return False


def print_engine_guidance(gpu: bool, cfg: Config, log: logging.Logger):
    log.info("-" * 60)
    if gpu:
        log.info("检测到高性能显卡，推荐本地模型方案：")
        log.info("  1. MinerU（PDF 解析，本地 GPU 推理）")
        log.info("     conda create -n mineru_env python=3.10")
        log.info("     conda activate mineru_env")
        log.info("     pip install mineru")
        log.info("     首次运行会自动下载模型（或设置 MINERU_MODEL_SOURCE=modelscope）")
        log.info("  2. 本地视觉模型（可选，替代云端 OCR）")
        log.info("     安装 Ollama 后拉取视觉模型，例如: ollama pull qwen2.5vl:7b")
    else:
        log.info("未检测到高性能显卡，推荐云端 OCR 方案（PaddleOCR 云 API，首选）：")
    log.info("开启云端 OCR 的步骤：")
    log.info("  1. 编辑 kb_config.toml，在 [cloud_ocr] 下设置 enabled = true、provider = \"paddle\"")
    log.info("  2. 设置环境变量: set PADDLE_OCR_API_KEY=你的token")
    log.info("  3. 可选（更稳定）：设置环境变量 MINERU_API_KEY=你的token，")
    log.info("     并在 [cloud_ocr] 设置 fallback_providers = [\"mineru\"]，")
    log.info("     PaddleOCR 失败时自动切换到 MinerU 云端（或用 kb ocr enable --provider paddle --fallback mineru）")
    log.info("  4. 预演: kb convert --engine cloud --dry-run")
    log.info("  5. 执行: kb convert")
    log.info("风险提示：云端 OCR 会产生 API 费用，且文档图片会发送到第三方服务；")
    log.info("         必须显式在配置中启用，程序默认关闭。")


def run_setup(cfg: Config, logger: logging.Logger | None = None,
              venv_dir: Path | None = None) -> int:
    log = logger or logging.getLogger("kbimporter")
    log.info("=" * 60)
    log.info("kb setup 安装引导")
    log.info("=" * 60)

    # 第 0 步：先扫描本机环境，再决定装什么
    log.info("第 0 步：扫描本机环境...")
    from kbimporter.doctor import run_doctor
    run_doctor(cfg, logger=log)

    project_root = _project_root()
    log.info(f"项目目录: {project_root}")
    venv_dir = venv_dir or (project_root / ".venv")

    if sys.stdin.isatty():
        gpu = _ask_gpu()
        print_engine_guidance(gpu, cfg, log)
        scheme = _ask_ocr_scheme()
        extras = scheme_extras(scheme)
        log.info(
            f"按当前方案安装 extras: [{','.join(extras)}]"
            + ("（含本地转换引擎 marker-pdf，体积较大）" if scheme in ("1", "3") else "")
        )

        status = venv_status(venv_dir)
        if status == "exists_with_kb":
            log.info(f"检测到已存在的虚拟环境，且已安装 kb: {venv_dir}（直接复用）")
            if _ask_yes_no("更新 kbimporter，并按当前方案补装依赖到该环境吗？", default=False):
                install_into_venv(venv_dir, extras, project_root, log)
        elif status == "exists_without_kb":
            log.info(f"检测到已存在的虚拟环境（尚未安装 kb）: {venv_dir}")
            if _ask_yes_no("按当前方案安装 kbimporter 及依赖到该环境吗？（推荐）", default=True):
                install_into_venv(venv_dir, extras, project_root, log)
        else:
            if _ask_yes_no(f"创建虚拟环境 {venv_dir} 吗？", default=True):
                if not create_venv(venv_dir, log):
                    return 1
                if _ask_yes_no("按当前方案安装 kbimporter 及依赖到该虚拟环境吗？（推荐）", default=True):
                    install_into_venv(venv_dir, extras, project_root, log)

        if scheme in ("2", "3"):
            cfg_path = cfg.config_path or (project_root / DEFAULT_CONFIG_FILE)
            if cfg_path.exists():
                if _ask_yes_no(f"是否修改 {cfg_path} 启用云端 OCR（PaddleOCR）？", default=True):
                    ok1 = _set_config_value(cfg_path, "cloud_ocr.enabled", True)
                    ok2 = _set_config_value(cfg_path, "cloud_ocr.provider", "paddle")
                    if ok1 or ok2:
                        log.info("已写入配置: [cloud_ocr] enabled = true, provider = \"paddle\"")
                    else:
                        log.warning("未找到 [cloud_ocr] 配置段，请手动编辑配置文件")
            else:
                log.info("尚未生成配置文件，请先运行: kb init --root <知识库路径>")
            log.info("还需设置环境变量: set PADDLE_OCR_API_KEY=你的token")
            log.info("然后运行: kb convert --engine cloud --dry-run 预演")
        else:
            log.info("保持本地引擎优先（marker -> mineru），云端 OCR 保持关闭。")
            log.info("以后想开启云端 OCR：编辑 kb_config.toml 把 [cloud_ocr] enabled 改为 true 并设置 PADDLE_OCR_API_KEY。")
    else:
        log.info("非交互模式：仅打印引导信息。")
        status = venv_status(venv_dir)
        local_hint = _pip_hint(venv_dir, scheme_extras("1"))
        cloud_hint = _pip_hint(venv_dir, scheme_extras("2"))
        hybrid_hint = _pip_hint(venv_dir, scheme_extras("3"))
        if status == "exists_with_kb":
            log.info(f"  检测到虚拟环境已存在且已安装 kb: {venv_dir}")
            log.info(f"  按需补装依赖: {local_hint}（本地/混合）或 {cloud_hint}（云端）")
        elif status == "exists_without_kb":
            log.info(f"  检测到虚拟环境已存在（未安装 kb）: {venv_dir}")
            log.info(f"  本地引擎方案: cd {project_root} && {local_hint}")
            log.info(f"  云端 OCR 方案: cd {project_root} && {cloud_hint}")
            log.info(f"  混合方案:     cd {project_root} && {hybrid_hint}")
        else:
            log.info(f"  1. python -m venv {venv_dir}")
            log.info(f"  2. 本地引擎方案: cd {project_root} && {local_hint}")
            log.info(f"     云端 OCR 方案: cd {project_root} && {cloud_hint}")
            log.info(f"     混合方案:     cd {project_root} && {hybrid_hint}")
        log.info("  3. 运行 kb doctor 检查环境")
        log.info("  4. OCR 方案：有显卡用 MinerU 本地模型；无显卡用 PaddleOCR 云 API（见 README）")

    log.info("下一步：`kb doctor` 体检，`kb status` 查看知识库，`kb import --dry-run` 预演导入。")
    return 0
