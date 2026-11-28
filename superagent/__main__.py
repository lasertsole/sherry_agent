"""``python -m superagent`` process entry point.

Delegates entirely to :mod:`superagent.index` so the lifecycle is unit-testable.
"""

from __future__ import annotations

from superagent.index import main

if __name__ == "__main__":
    raise SystemExit(main())
