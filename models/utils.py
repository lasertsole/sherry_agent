"""Shared utilities for the models/ packages (audit 1.1 dedup)."""

import re

from config import ENV_PATH


def read_env_file_value(key: str, default: str = "", env_path=None) -> str:
    """Parse a value from the .env file only, avoiding os.environ.

    Deliberately does NOT read ``os.environ`` or call ``load_dotenv``: callers
    (embed/reranker model configuration) must observe exactly what the .env
    file says, without load_dotenv side effects. The first matching
    non-empty assignment wins; surrounding whitespace and one level of
    matching quotes are stripped; unreadable/missing files yield ``default``.

    Uses ``[ \\t]*`` (never ``\\s*``) around ``=`` so a match cannot cross a
    newline — the original duplicated implementations swallowed the NEXT
    line of the file when a key had an empty value (``EMPTY=`` + next line
    returned the next line as the value).
    """
    path = env_path if env_path is not None else ENV_PATH
    try:
        text = path.read_text(encoding="utf-8")
        for mobj in re.finditer(
            rf"^[ \t]*(?:export[ \t]+)?{re.escape(key)}[ \t]*=[ \t]*(.*?)[ \t]*$",
            text,
            re.MULTILINE,
        ):
            raw = mobj.group(1)
            raw = raw.strip("\"'").strip()
            if raw:
                return raw
    except Exception:
        pass
    return default


def resolve_gguf_path(
    local_path, hf_repo: str, hf_filename: str, local_dir, fallback_path=None
) -> str:
    """Resolve a GGUF weight path: local hit -> fallback copy -> HF download.

    Shared by auxiliary_llm / ITTT_model / VTTT_model (audit 1.1.3). The
    per-model modules keep thin wrappers passing their own closure constants.

    Args:
        local_path: Target gguf file path (usually local_dir / hf_filename).
        hf_repo: Hugging Face repo id for the download fallback.
        hf_filename: Filename inside the HF repo.
        local_dir: Directory that receives the download / fallback copy.
        fallback_path: Optional alternate local copy to clone from (auxiliary
            model_weight dir) before hitting the network.
    """
    import shutil
    from pathlib import Path as _Path

    local = _Path(local_path)
    if local.is_file():
        return str(local)

    if fallback_path is not None:
        fallback = _Path(fallback_path)
        if fallback.is_file():
            print(f"Copying GGUF from {fallback} -> {local} ...")
            _Path(local_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(fallback), str(local))
            return str(local)

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise ImportError(
            "Model file not found locally and 'huggingface_hub' is not installed. "
            "Run: pip install huggingface_hub"
        ) from None

    print(f"Downloading {hf_repo}/{hf_filename} -> {local_dir} ...")
    hf_hub_download(
        repo_id=hf_repo,
        filename=hf_filename,
        local_dir=str(local_dir),
    )
    return str(local)
