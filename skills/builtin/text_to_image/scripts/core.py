"""Text-to-image skill script: generate, persist as a session artifact, print URIs.

The model rules (per-model image cap, the older ``response_format`` guard, URL
vs base64 responses) live in :mod:`models.TTI_model` — the same provider the
main-agent ``image_generation`` tool uses, so the two routes cannot drift. This
script is the terminal-run half:

* it addresses the session-scoped artifact store through ``SHERRY_SESSION_ID``
  (injected by the terminal tool), so identical bytes deduplicate and the file
  disappears with its session;
* it PRINTS every ``sherry-artifact://`` URI to stdout — the terminal tool
  captures stdout, so this is how the agent learns where the image went (the
  previous version only logged to stderr and the tool result was empty);
* it fails loudly in stdout (``[error] ...``) instead of swallowing the
  exception, so the agent can react instead of guessing.

Run it through the terminal tool; calling it outside a session has no artifact
namespace and is refused.
"""

import sys
from pathlib import Path

# Dynamically add project root to sys.path (every scripted skill does this).
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os  # noqa: E402

from loguru import logger  # noqa: E402
from pydantic import validate_call  # noqa: E402

from agent.tools.pub_base.artifact_store import write_artifact  # noqa: E402
from config.path import load_env  # noqa: E402
from models.TTI_model import TTIError, TTI_model  # noqa: E402

# .env plus the .models / .network overlays (the TTI_* keys live there).
load_env()


def _session_id() -> str:
    """The session the terminal tool tagged this child process with."""
    return str(os.getenv("SHERRY_SESSION_ID") or "").strip()


def _report_error(message: str) -> str:
    """Print a failure to stdout (the terminal result) and answer ``""``."""
    print(message)
    logger.warning(message)
    return ""


@validate_call
def generate_image(
    prompt: str,
    *,
    size: str | None = None,
    quality: str | None = None,
    n: int = 1,
    model: str | None = None,
) -> str:
    """Generate image(s) from a text prompt and store them in the artifact store.

    Args:
        prompt: Text description of the image to generate.
        size: Provider image size (default: the provider's own default).
        quality: Provider quality (``standard``/``hd`` for DALL·E, ``auto``/
            ``low``/``medium``/``high`` for gpt-image models).
        n: How many images to request; the provider's per-model cap and
            ``IMAGE_GEN["max_n"]`` clamp it (``dall-e-3`` always returns one).
        model: Override the configured ``TTI_API_NAME`` for this call.

    Returns:
        The ``sherry-artifact://`` URI(s), one per line ("" on failure).
        Every URI is also printed so the terminal tool result carries it.
    """
    session_id = _session_id()
    if not session_id:
        return _report_error(
            "[error] SHERRY_SESSION_ID is not set — run this script through the "
            "terminal tool so generated images can be stored in the session "
            "artifact store."
        )
    try:
        images = TTI_model.generate(
            prompt,
            size=size,
            quality=quality,
            n=n,
            model=model,
        )
    except TTIError as error:
        return _report_error(f"[error] image generation failed: {error}")
    except Exception as error:  # noqa: BLE001 - the script boundary reports text
        logger.exception("text_to_image: unexpected failure")
        return _report_error(f"[error] image generation failed: {type(error).__name__}: {error}")

    uris: list[str] = []
    discarded = 0
    for image in images:
        uri = write_artifact(session_id, image.data, media_type="image", ext=image.ext)
        if uri is None:
            discarded += 1
            continue
        uris.append(uri)
        print(uri)
    if not uris:
        return _report_error(
            "[error] the image(s) were generated but could not be persisted "
            "(the artifact store is unavailable)."
        )
    if discarded:
        print(f"[warn] {discarded} generated image(s) could not be persisted and were discarded.")
    logger.info(
        "text_to_image: stored {} image(s) for session {}",
        len(uris),
        session_id,
    )
    return "\n".join(uris)
