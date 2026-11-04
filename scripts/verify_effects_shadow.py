"""Print a shadow-mode consistency snapshot for the effect queue.

Run after ``effects.enabled=true`` + ``effects.shadow_mode=true`` has observed
real traffic. The effects database is read-only here; for completion notices it
is compared with the subagent registry's own DELIVERED rows, which is the
persistent oracle the shadow recorder can be checked against. Continuation and
auto-turn intents have no second persistent oracle (their state is in-process),
so their counts are printed for inspection rather than asserted.

Exit code is 0 even when the databases are absent: this is a diagnostic, not a
gate.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.path import APP_HOME  # noqa: E402 - repo root added above

_EFFECTS_DB = APP_HOME / "src" / "data" / "effects" / "effects.db"
_REGISTRY_DB = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "tools"
    / "subagent"
    / "data"
    / "subagent_registry.db"
)


def _counts(db_path: Path, sql: str) -> Counter:
    if not db_path.is_file():
        return Counter()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            return Counter(tuple(row) for row in conn.execute(sql))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a diagnostic never crashes
        print(f"  ! {db_path}: {exc}")
        return Counter()


def _delivered_registry_runs(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    delivered = 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            for (raw,) in conn.execute("SELECT data FROM subagent_runs"):
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if str(data.get("delivery", {}).get("status", "")) == "delivered":
                    delivered += 1
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {db_path}: {exc}")
    return delivered


def main() -> None:
    effects = _counts(_EFFECTS_DB, "SELECT kind, status FROM effect_intent")
    print("effects intent status counts:")
    for (kind, status), count in sorted(effects.items()):
        print(f"  {kind:20s} {status:10s} {count}")

    completion = Counter(
        (status, count) for (kind, status), count in effects.items() if kind == "completion_notice"
    )
    done = sum(count for status, count in completion.items() if status == "done")
    delivered = _delivered_registry_runs(_REGISTRY_DB)
    print(
        f"completion shadow: effects done={done}, registry delivered={delivered}"
        + (f", delta={done - delivered:+d}" if (done or delivered) else "")
    )
    print("continuation/auto_turn: no persistent oracle; inspect the counts above.")


if __name__ == "__main__":
    main()
