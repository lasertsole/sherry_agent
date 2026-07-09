"""
Ensure MinerU pipeline models are downloaded and configured.

Can be imported and called programmatically or run as a standalone script.

Usage (standalone):
    python tests/scripts/ensure_mineru_models.py

Usage (import):
    from tests.scripts.ensure_mineru_models import ensure_mineru_models
    ensure_mineru_models(source="huggingface")  # or "modelscope"
"""

import os
import json
import shutil
import subprocess
from pathlib import Path
from loguru import logger
from config import MODELS_DIR

# ──────────────────────────────────────────────
# 0. 项目路径
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_DIR = PROJECT_ROOT / ".venv"
MINERU_DOWNLOAD_SCRIPT = VENV_DIR / "Scripts" / "mineru-models-download.exe"

# 模型存放目录（项目内）
EXTRACT_MODELS_DIR = MODELS_DIR / "extract_model"

# 项目内 MinerU 配置文件路径
PROJECT_CONFIG_FILE = EXTRACT_MODELS_DIR / "mineru_config.json"

HOME_DIR = Path.home()
USER_CONFIG_FILE = HOME_DIR / "mineru.json"

# ──────────────────────────────────────────────
# 1. 配置读写
# ──────────────────────────────────────────────
def _read_config(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_models_dir_from_config(config: dict, model_type: str = "pipeline") -> str | None:
    return config.get("models-dir", {}).get(model_type)


def _update_models_dir(config: dict, model_type: str, path: str) -> dict:
    if "models-dir" not in config:
        config["models-dir"] = {}
    config["models-dir"][model_type] = path
    return config


def _ensure_config_has_version(config: dict) -> dict:
    if "config_version" not in config:
        config["config_version"] = "1.3.1"
    return config


# ──────────────────────────────────────────────
# 2. 模型发现（先查项目内，再查缓存）
# ──────────────────────────────────────────────
def _find_model_in_project(model_type: str) -> str | None:
    """Check if model already exists in project's models/ directory."""
    model_dir = EXTRACT_MODELS_DIR / model_type
    if model_dir.exists() and any(model_dir.iterdir()):
        return str(model_dir)
    return None


def _find_modelscope_cache_dir(model_name: str, model_type_label: str) -> str | None:
    """Find a modelscope cached model directory matching the given model name.

    Args:
        model_name: Name like "PDF-Extract-Kit-1___0" or "MinerU2.5-Pro-2605-1.2B".
        model_type_label: Label for logging ("pipeline" or "vlm").

    Returns:
        Absolute directory path, or None if not found.
    """
    ms_cache_root = Path.home() / ".cache" / "modelscope" / "hub" / "models" / "OpenDataLab"
    if not ms_cache_root.exists():
        return None

    for candidate in ms_cache_root.iterdir():
        if candidate.is_dir() and model_name in candidate.name:
            snapshots_dir = candidate / "snapshots"
            if snapshots_dir.exists():
                snapshots = list(snapshots_dir.iterdir())
                if snapshots:
                    snapshot_dir = max(snapshots, key=lambda p: p.stat().st_mtime)
                    logger.debug(f"Found {model_type_label} model in modelscope cache: {snapshot_dir}")
                    return str(snapshot_dir)
            logger.debug(f"Found {model_type_label} model in modelscope cache (no snapshots): {candidate}")
            return str(candidate)

    return None


def _find_hf_cache_dir(hf_model_name: str, model_type_label: str) -> str | None:
    """Find a huggingface cached model directory matching the given model name.

    Args:
        hf_model_name: Name like "opendatalab--PDF-Extract-Kit-1.0" (with double dash).
        model_type_label: Label for logging.

    Returns:
        Absolute directory path, or None if not found.
    """
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    if not hf_cache.exists():
        return None

    models_dir = hf_cache / f"models--{hf_model_name}"
    if not models_dir.exists():
        return None

    snapshots_dir = models_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    snapshots = list(snapshots_dir.iterdir())
    if not snapshots:
        return None

    snapshot_dir = max(snapshots, key=lambda p: p.stat().st_mtime)
    logger.debug(f"Found {model_type_label} model in huggingface cache: {snapshot_dir}")
    return str(snapshot_dir)


def _resolve_cache_path(source: str, model_type: str) -> str:
    """Resolve a model's cache directory, trying huggingface or modelscope."""
    if model_type == "pipeline":
        hf_name = "opendatalab--PDF-Extract-Kit-1.0"
        ms_name = "PDF-Extract-Kit-1___0"
    elif model_type == "vlm":
        hf_name = "opendatalab--MinerU2.5-Pro-2605-1.2B"
        ms_name = "MinerU2.5-Pro-2605-1.2B"
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

    path = _find_hf_cache_dir(hf_name, model_type)
    if path:
        return path

    path = _find_modelscope_cache_dir(ms_name, model_type)
    if path:
        return path

    raise RuntimeError(
        f"MinerU {model_type} model not found in any cache after download. "
        f"Searched huggingface ({hf_name}) and modelscope ({ms_name})."
    )


# ──────────────────────────────────────────────
# 3. 从缓存复制到项目目录
# ──────────────────────────────────────────────
def _copy_cache_to_project(model_type: str, cache_path: str) -> str:
    """Copy model from cache directory to project models/ directory.

    Returns the project-internal target path.
    """
    target_dir = EXTRACT_MODELS_DIR / model_type
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.debug(f"Copying {model_type} model from cache to project: {target_dir} ...")
    for item in Path(cache_path).iterdir():
        dest = target_dir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    logger.debug(f"Copied {model_type} model to {target_dir}")
    return str(target_dir)


# ──────────────────────────────────────────────
# 4. 下载 + 复制到项目目录
# ──────────────────────────────────────────────
def _run_download_and_migrate(source: str, model_type: str) -> str:
    """Run mineru-models-download, then copy from cache to project dir.

    Falls back to modelscope if huggingface download fails.

    Returns the project-internal model path.
    """
    # 先检查是否已在项目内
    project_path = _find_model_in_project(model_type)
    if project_path:
        logger.debug(f"{model_type} model already exists in project: {project_path}")
        return project_path

    # 再检查是否有缓存（匹配指定的 source）
    try:
        cache_path = _resolve_cache_path(source, model_type)
        logger.debug(f"Found {model_type} model in cache: {cache_path}")
        return _copy_cache_to_project(model_type, cache_path)
    except RuntimeError:
        logger.debug(f"No cached {model_type} model found, downloading from {source} ...")

    # 下载（如果 huggingface 失败，fallback 到 modelscope）
    sources_to_try = [source]
    if source == "huggingface":
        sources_to_try.append("modelscope")

    for attempt_source in sources_to_try:
        logger.debug(f"Downloading MinerU {model_type} models from {attempt_source} ...")
        result = subprocess.run(
            [
                str(MINERU_DOWNLOAD_SCRIPT),
                "-s", attempt_source,
                "-m", model_type,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if result.returncode == 0:
            logger.debug(f"Download from {attempt_source} succeeded")
            if result.stdout:
                logger.debug(f"stdout:\n{result.stdout}")
            if result.stderr:
                logger.debug(f"stderr:\n{result.stderr}")

            # 下载后从缓存找到路径并复制到项目
            cache_path = _resolve_cache_path(attempt_source, model_type)
            return _copy_cache_to_project(model_type, cache_path)

        logger.warning(f"Download from {attempt_source} failed (exit code {result.returncode})")
        logger.warning(f"stdout: {result.stdout}")
        logger.warning(f"stderr: {result.stderr}")

    # 所有源都失败
    raise RuntimeError(
        f"MinerU {model_type} model download failed. "
        f"Tried sources: {sources_to_try}. "
        f"Check network connectivity."
    )


# ──────────────────────────────────────────────
# 5. 更新配置文件
# ──────────────────────────────────────────────
def _write_mineru_configs(model_type: str, project_model_path: str) -> None:
    """Update both project-local and user-homedir config pointing to project model path."""
    # 更新项目内配置文件
    config = _read_config(PROJECT_CONFIG_FILE)
    config = _update_models_dir(config, model_type, project_model_path)
    config = _ensure_config_has_version(config)
    _write_config(PROJECT_CONFIG_FILE, config)
    logger.debug(f"Updated {PROJECT_CONFIG_FILE}")

    # 同时也更新 ~/mineru.json 保持一致性
    user_config = _read_config(USER_CONFIG_FILE)
    user_config = _update_models_dir(user_config, model_type, project_model_path)
    user_config = _ensure_config_has_version(user_config)
    _write_config(USER_CONFIG_FILE, user_config)
    logger.debug(f"Updated {USER_CONFIG_FILE}")


# ──────────────────────────────────────────────
# 6. Public entry point
# ──────────────────────────────────────────────
def ensure_mineru_models(source: str = "huggingface", download_vlm: bool = True) -> dict:
    """Ensure MinerU pipeline (and optionally VLM) models are in the project directory.

    Strategy:
    1. Check if model already exists in project models/extract_model/{pipeline,vlm}/
    2. If not, check huggingface/modelscope cache and copy to project
    3. If not in cache, download and then copy to project
    4. Update both project-level mineru_config.json and ~/mineru.json

    Args:
        source: Model download source. "huggingface" (default) or "modelscope".
        download_vlm: Whether to also handle the VLM model (default: True).

    Returns:
        Dict with keys "pipeline" and "vlm" mapping to local model root paths.
        "vlm" may be None if not configured.

    Raises:
        RuntimeError: If download or configuration fails.
    """
    if source not in ("huggingface", "modelscope"):
        raise ValueError(f"source must be 'huggingface' or 'modelscope', got {source!r}")

    EXTRACT_MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Handle pipeline models
    pipeline_path = _run_download_and_migrate(source, model_type="pipeline")
    _write_mineru_configs("pipeline", pipeline_path)

    # 2. Optionally handle VLM models
    vlm_path = None
    if download_vlm:
        try:
            vlm_path = _run_download_and_migrate(source, model_type="vlm")
            _write_mineru_configs("vlm", vlm_path)
        except RuntimeError as e:
            logger.warning(f"VLM model handling failed (continuing without VLM): {e}")

    logger.debug(f"MinerU models configured: pipeline={pipeline_path}, vlm={vlm_path or '(not configured)'}")
    return {"pipeline": pipeline_path, "vlm": vlm_path}


# ──────────────────────────────────────────────
# 7. Helper to check if VLM is ready
# ──────────────────────────────────────────────
def is_vlm_configured() -> bool:
    """Check if VLM model is present in the project directory."""
    project_path = _find_model_in_project("vlm")
    if project_path:
        return True
    # Check any config
    for cfg_path in [PROJECT_CONFIG_FILE, USER_CONFIG_FILE]:
        config = _read_config(cfg_path)
        vlm_path = _get_models_dir_from_config(config, "vlm")
        if vlm_path and Path(vlm_path).exists():
            return True
    return False


# ──────────────────────────────────────────────
# 8. CLI entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ensure MinerU pipeline & VLM models are in the project directory.")
    parser.add_argument(
        "-s", "--source",
        choices=["huggingface", "modelscope"],
        default="huggingface",
        help="Model download source (default: huggingface)",
    )
    parser.add_argument(
        "--no-vlm",
        action="store_true",
        help="Skip VLM model handling",
    )
    args = parser.parse_args()

    print(f"Using source: {args.source}, download_vlm={not args.no_vlm}")
    result = ensure_mineru_models(source=args.source, download_vlm=not args.no_vlm)
    print(f"Done. Pipeline: {result['pipeline']}")
    if result.get("vlm"):
        print(f"       VLM:     {result['vlm']}")
