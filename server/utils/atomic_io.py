"""Shared atomic file-write helper (audit 2.1.8).

Four server modules previously implemented the same "tempfile in the target's
directory + ``os.replace``" write pattern (channel config, SkillSpector scan
cache, skills state, and — partially — the .env writer). This helper owns the
shared mechanics; the serialization format and the error-handling/logging
policy stay at the call sites (the helper re-raises so callers keep their
exact return-value/log behavior).

Scope note (2026-09-06): ``server/service/env.py::write_env_file`` was listed
in audit item 2.1.8 but does NOT use the tempfile+replace pattern — it writes
a ``.bak`` backup and then rewrites the file in place. Converting it would
change its crash/backup semantics, so it is deliberately left as-is.
"""

import os
import tempfile
from pathlib import Path
from typing import Union

from loguru import logger


def atomic_write_text(path: Union[str, Path], text: str, *, fsync: bool = False) -> None:
    """Atomically write *text* to *path*; raises on failure.

    Writes to a temporary file created in the same directory as the target
    (guaranteed same filesystem, so the final ``os.replace`` is atomic), then
    ``os.replace()``s it into place — a failed/interrupted write never leaves
    a truncated target behind.

    Args:
        path: Target file path; missing parent directories are created.
        text: UTF-8 text payload.
        fsync: When ``True``, flush + ``os.fsync`` the temp file before the
            replace (durability for small config files that must survive a
            crash; the channel-config writer historically fsynced, the cache/
            state writers did not).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.tmp.", suffix=""
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_name, str(target))
    except BaseException:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.warning("atomic_write_text: failed to remove temp file {}", tmp_name)
        raise
