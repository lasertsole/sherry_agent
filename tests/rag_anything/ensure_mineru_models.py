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
import sys
import json
import subprocess
from pathlib import Path
from loguru import logger

# ──────────────────────────────────────────────
# 0. 项目路径
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENV_DIR = PROJECT_ROOT / ".venv"
MINERU_DOWNLOAD_SCRIPT = VENV_DIR / "Scripts" / "mineru-models-download.exe"

HOME_DIR = Path.home()
MINERU_CONFIG_FILE = HOME_DIR / "mineru.json"


# ──────────────────────────────────────────────
# 1. 判断是否已下载
# ──────────────────────────────────────────────
def _is_models_configured(model_type: str = "pipeline") -> bool:
    """Check if ~/mineru.json exists and points to an existing model dir."""
    if not MINERU_CONFIG_FILE.exists():
        return False
    try:
        config = json.loads(MINERU_CONFIG_FILE.read_text(encoding="utf-8"))
        model_dir = config.get("models-dir", {}).get(model_type)
        if not model_dir:
            return False
        return Path(model_dir).exists()
    except (json.JSONDecodeError, KeyError, TypeError):
        return False


def _get_local_model_root(model_type: str = "pipeline") -> str | None:
    """Read the configured local model root from ~/mineru.json."""
    if not MINERU_CONFIG_FILE.exists():
        return None
    try:
        config = json.loads(MINERU_CONFIG_FILE.read_text(encoding="utf-8"))
        return config.get("models-dir", {}).get(model_type)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ──────────────────────────────────────────────
# 2. 下载 + 配置（静默，非交互）
# ──────────────────────────────────────────────
def _run_download(source: str, model_type: str = "pipeline") -> None:
    """Run mineru-models-download non-interactively.

    The CLI might prompt interactively when options are not supplied.
    We pass both --source and --model_type to avoid prompts.
    """
    logger.info(f"Downloading MinerU {model_type} models from {source} ...")
    result = subprocess.run(
        [
            str(MINERU_DOWNLOAD_SCRIPT),
            "-s", source,
            "-m", model_type,
        ],
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute timeout for large model downloads
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if result.returncode != 0:
        logger.error(f"Download failed (exit code {result.returncode})")
        logger.error(f"stdout: {result.stdout}")
        logger.error(f"stderr: {result.stderr}")
        raise RuntimeError(
            f"mineru-models-download failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    logger.info(f"Download stdout:\n{result.stdout}")
    if result.stderr:
        logger.info(f"Download stderr:\n{result.stderr}")


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

    # Search for any directory whose name contains the model name
    for candidate in ms_cache_root.iterdir():
        if candidate.is_dir() and model_name in candidate.name:
            # Look for snapshots subdirectory
            snapshots_dir = candidate / "snapshots"
            if snapshots_dir.exists():
                snapshots = list(snapshots_dir.iterdir())
                if snapshots:
                    # Use the most recent snapshot
                    snapshot_dir = max(snapshots, key=lambda p: p.stat().st_mtime)
                    logger.info(f"Found {model_type_label} model in modelscope cache: {snapshot_dir}")
                    return str(snapshot_dir)
            # If no snapshots subdir, use the candidate dir itself
            logger.info(f"Found {model_type_label} model in modelscope cache (no snapshots): {candidate}")
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
    logger.info(f"Found {model_type_label} model in huggingface cache: {snapshot_dir}")
    return str(snapshot_dir)


# VLM model identifiers (HF name -> modelscope name fragment)
_VLM_HF_NAME = "opendatalab--MinerU2.5-Pro-2605-1.2B"
_VLM_MC_NAME = "MinerU2.5-Pro-2605-1.2B"


def _resolve_vlm_model_path(source: str) -> str:
    """Resolve the local VLM model path from cache, trying both huggingface and modelscope.

    Returns:
        Absolute path to the VLM model directory.
    """
    path = _find_hf_cache_dir(_VLM_HF_NAME, "vlm")
    if path:
        return path

    path = _find_modelscope_cache_dir(_VLM_MC_NAME, "vlm")
    if path:
        return path

    raise RuntimeError(
        f"MinerU VLM model not found in cache after download. "
        f"Searched huggingface ({_VLM_HF_NAME}) and modelscope ({_VLM_MC_NAME}). "
        f"The download may have failed."
    )


def _resolve_pipeline_model_path(source: str) -> str:
    """Resolve the local pipeline model path from cache, trying both huggingface and modelscope.

    Returns:
        Absolute path to the pipeline model directory.
    """
    path = _find_hf_cache_dir("opendatalab--PDF-Extract-Kit-1.0", "pipeline")
    if path:
        return path

    path = _find_modelscope_cache_dir("PDF-Extract-Kit-1___0", "pipeline")
    if path:
        return path

    raise RuntimeError(
        "MinerU pipeline model not found in cache after download. "
        "The download may have failed."
    )


def _ensure_mineru_config_json(source: str = "modelscope") -> None:
    """Ensure ~/mineru.json has valid pipeline and vlm entries.

    Reads existing config (preserving any existing vlm entry),
    then resolves pipeline and vlm paths from cache.
    """
    config = {"models-dir": {}}
    if MINERU_CONFIG_FILE.exists():
        try:
            existing = json.loads(MINERU_CONFIG_FILE.read_text(encoding="utf-8"))
            config = existing
        except (json.JSONDecodeError, TypeError):
            pass

    # Ensure models-dir section exists
    if "models-dir" not in config:
        config["models-dir"] = {}

    # Resolve pipeline path
    pipeline_root = _resolve_pipeline_model_path(source)
    config["models-dir"]["pipeline"] = pipeline_root

    # Resolve VLM path (if downloaded, preserve it if already set)
    try:
        vlm_root = _resolve_vlm_model_path(source)
        config["models-dir"]["vlm"] = vlm_root
    except RuntimeError as e:
        logger.warning(f"VLM model not found: {e}")
        # Keep existing vlm value if set
        if not config.get("models-dir", {}).get("vlm"):
            config["models-dir"]["vlm"] = ""

    MINERU_CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote ~/mineru.json: pipeline={config['models-dir']['pipeline']}, vlm={config['models-dir'].get('vlm', '')}")


def _remove_existing_mineru_json() -> None:
    """Remove ~/mineru.json if it exists (to avoid stale/incomplete config)."""
    if MINERU_CONFIG_FILE.exists():
        MINERU_CONFIG_FILE.unlink()
        logger.info(f"Removed existing {MINERU_CONFIG_FILE}")


# ──────────────────────────────────────────────
# 3. Public entry point
# ──────────────────────────────────────────────
def ensure_mineru_models(source: str = "huggingface", download_vlm: bool = True) -> dict:
    """Ensure MinerU pipeline (and optionally VLM) models are downloaded and configured.

    Args:
        source: Model download source. "huggingface" (default) or "modelscope".
        download_vlm: Whether to also download the VLM model (default: True).

    Returns:
        Dict with keys "pipeline" and "vlm" mapping to local model root paths.
        "vlm" may be empty string if not downloaded.

    Raises:
        RuntimeError: If download or configuration fails.
    """
    if source not in ("huggingface", "modelscope"):
        raise ValueError(f"source must be 'huggingface' or 'modelscope', got {source!r}")

    # Remove stale mineru.json to trigger fresh detection after download
    _remove_existing_mineru_json()

    # 1. Download pipeline models
    _run_download(source, model_type="pipeline")

    # 2. Optionally download VLM models
    if download_vlm:
        try:
            _run_download(source, model_type="vlm")
        except RuntimeError as e:
            logger.warning(f"VLM model download failed (continuing without VLM): {e}")

    # 3. Update ~/mineru.json with discovered paths
    _ensure_mineru_config_json(source=source)

    pipeline_root = _get_local_model_root("pipeline")
    if not pipeline_root:
        raise RuntimeError("Failed to determine pipeline model root after download.")

    vlm_root = _get_local_model_root("vlm")

    logger.info(f"MinerU models configured: pipeline={pipeline_root}, vlm={vlm_root or '(not configured)'}")
    return {"pipeline": pipeline_root, "vlm": vlm_root or ""}


# ──────────────────────────────────────────────
# 4. Helper to check if VLM is ready
# ──────────────────────────────────────────────
def is_vlm_configured() -> bool:
    """Check if VLM model is present and configured in ~/mineru.json."""
    return _is_models_configured("vlm")


# ──────────────────────────────────────────────
# 5. CLI entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ensure MinerU pipeline & VLM models are downloaded and configured.")
    parser.add_argument(
        "-s", "--source",
        choices=["huggingface", "modelscope"],
        default="huggingface",
        help="Model download source (default: huggingface)",
    )
    parser.add_argument(
        "--no-vlm",
        action="store_true",
        help="Skip VLM model download",
    )
    args = parser.parse_args()

    print(f"Using source: {args.source}, download_vlm={not args.no_vlm}")
    result = ensure_mineru_models(source=args.source, download_vlm=not args.no_vlm)
    print(f"Done. Pipeline: {result['pipeline']}")
    if result.get("vlm"):
        print(f"       VLM:     {result['vlm']}")
