"""Sync the litellm model-price JSON into the local cache (B-layer pricing).

``config/features/infra_side/model_pricing.py`` reads
``src/data/litellm_prices.json`` when the user's ``.models`` price table has no
entry for a model. This script refreshes that cache from litellm's community
``model_prices_and_context_window.json``; it is a maintenance tool, not part of
the runtime. The cache is gitignored (``/src/``), so run it on the machine that
serves the backend and restart (or let the next process load it).

Usage::

    uv run --no-sync python scripts/sync_litellm_prices.py
    uv run --no-sync python scripts/sync_litellm_prices.py --url <mirror>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.path import SRC_DIR  # noqa: E402

DEFAULT_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="source JSON URL")
    parser.add_argument(
        "--output",
        default=str(Path(SRC_DIR) / "data" / "litellm_prices.json"),
        help="cache path (default: src/data/litellm_prices.json)",
    )
    args = parser.parse_args()

    request = urllib.request.Request(args.url, headers={"User-Agent": "sherry-agent"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    data = json.loads(raw)
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"source did not return a non-empty JSON object: {args.url}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(output.name + ".part")
    part.write_bytes(raw)
    part.replace(output)
    print(f"wrote {len(data)} model price entries to {output}")


if __name__ == "__main__":
    main()
