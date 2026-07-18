"""Test: nudge agent produces valid XpGraph JSON from _SKILL_REVIEW_PROMPT.

Exercises the real agent with a sample conversation to verify:
1. The agent outputs valid JSON
2. The JSON has the expected structure (nodes + edges)
3. Node types are correct (SKILL/EVENT/TASK)
"""

import pytest
import json
import os
import sys
from typing import Any

# Ensure project root is on sys.path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ─── Patch heavy dependencies early ───────────────────────────────

# Patch state_register_mem BEFORE any real imports touch it
import runtime.state_register
from unittest.mock import MagicMock, AsyncMock, patch

mock_mem = MagicMock()
mock_mem.set_state = MagicMock()
mock_mem.get_state = MagicMock(return_value=None)
runtime.state_register.state_register_mem = mock_mem
runtime.state_register.state_register_db = MagicMock()

# Patch MesMemory add_messages to no-op
_path = "agent.middlewares.context_engine.core.add_messages"
with patch(_path, new_callable=AsyncMock) as _:
    pass  # will be patched in fixture too

from langchain_core.messages import HumanMessage, AIMessage
from agent.middlewares.context_engine.nudge import (
    _SKILL_REVIEW_PROMPT,
    _COMBINED_REVIEW_PROMPT,
    _create_nudge_agent,
)


# ─── Sample conversation (realistic tool-using session) ────────────

SAMPLE_MESSAGES = [
    HumanMessage(content="Create a Python script that scrapes product prices from a website and emails me when something drops below $50."),
    AIMessage(content="""I've created the price tracker. Here's what I built:

**price_tracker/tracker.py** — Main scraping logic using requests + BeautifulSoup
**price_tracker/notifier.py** — Email notification via smtplib
**price_tracker/config.py** — Product URLs, threshold prices, email settings
**price_tracker/requirements.txt** — Dependencies

The script:
1. Fetches product pages with requests (with retry logic + user-agent rotation)
2. Parses HTML with BeautifulSoup to extract prices
3. Compares against configured thresholds
4. Sends an HTML-formatted email with matched products

To set it up:
```bash
cd price_tracker
pip install -r requirements.txt
# Edit config.py with your products and email settings
python tracker.py
```"""),
    HumanMessage(content="Thanks! But can you make it so the config is a YAML file instead of a Python file? It's easier for me to edit."),
    AIMessage(content="Good idea — YAML config is much more user-friendly. Let me refactor."),
    HumanMessage(content="Also, don't explain so much in your responses. Just give me the code and a brief what-it-does. I know what I'm doing."),
    AIMessage(content="Got it. Here's the refactored version with YAML config."),
    HumanMessage(content="Much better. Now I need to deploy this as a cron job on my server. What's the best way?"),
    AIMessage(
        content="""Quick setup:

1. Copy project to server
2. Install deps: `pip install -r requirements.txt`
3. Add crontab entry:
```bash
crontab -e
# Add: 0 9 * * * cd /path/to/price_tracker && python tracker.py
```

Make sure the YAML config has absolute paths for any file outputs.""",
    ),
]


@pytest.fixture(autouse=True)
def patch_deps():
    """Patch external dependencies (MesMemory, LLM) to avoid side effects."""
    # Patch MesMemory
    with (
        patch("agent.middlewares.context_engine.core.add_messages", new_callable=AsyncMock),
        patch("agent.middlewares.context_engine.nudge.state_register_mem", mock_mem),
    ):
        yield


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract JSON object from agent response text."""
    # Try parsing entire response
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON block enclosed in ```json ... ```
    import re
    m = re.search(r'```(?:json)?\s*\n?(\{.*?\})\n?\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find bare JSON object
    m = re.search(r'(\{.*"nodes".*"edges".*\})', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _validate_xpgraph_json(data: dict[str, Any]) -> list[str]:
    """Validate XpGraph JSON structure. Returns list of errors (empty = valid)."""
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append("Root is not a dict")
        return errors

    if "nodes" not in data:
        errors.append("Missing 'nodes' key")
    elif not isinstance(data["nodes"], list):
        errors.append("'nodes' is not a list")
    else:
        for i, node in enumerate(data["nodes"]):
            if not isinstance(node, dict):
                errors.append(f"nodes[{i}] is not a dict")
                continue
            if "type" not in node:
                errors.append(f"nodes[{i}] missing 'type'")
            elif node["type"] not in ("SKILL", "EVENT", "TASK"):
                errors.append(f"nodes[{i}].type='{node.get('type')}' not in (SKILL, EVENT, TASK)")
            if "name" not in node or not isinstance(node.get("name"), str) or not node["name"].strip():
                errors.append(f"nodes[{i}] missing or empty 'name'")
            if "description" not in node or not isinstance(node.get("description"), str):
                errors.append(f"nodes[{i}] missing or invalid 'description'")
            if "content" not in node or not isinstance(node.get("content"), str):
                errors.append(f"nodes[{i}] missing or invalid 'content'")

    if "edges" not in data:
        errors.append("Missing 'edges' key")
    elif not isinstance(data["edges"], list):
        errors.append("'edges' is not a list")
    else:
        valid_edge_types = {"USED_SKILL", "SOLVED_BY", "REQUIRES", "PATCHES", "CONFLICTS_WITH"}
        for i, edge in enumerate(data["edges"]):
            if not isinstance(edge, dict):
                errors.append(f"edges[{i}] is not a dict")
                continue
            if "type" not in edge:
                errors.append(f"edges[{i}] missing 'type'")
            elif edge["type"] not in valid_edge_types:
                errors.append(f"edges[{i}].type='{edge.get('type')}' not in {valid_edge_types}")
            if "from_name" not in edge or not isinstance(edge.get("from_name"), str):
                errors.append(f"edges[{i}] missing 'from_name'")
            if "to_name" not in edge or not isinstance(edge.get("to_name"), str):
                errors.append(f"edges[{i}] missing 'to_name'")

    return errors


@pytest.mark.asyncio
async def test_nudge_skill_extracts_valid_json():
    """_nudge_skill agent produces valid XpGraph JSON from sample conversation."""
    agent = await _create_nudge_agent("You are a helpful assistant.")

    res = await agent.ainvoke(input={
        "session_id": "test-nudge-skill",
        "messages": [
            *SAMPLE_MESSAGES,
            HumanMessage(content=_SKILL_REVIEW_PROMPT),
        ],
    })

    response_text = res["messages"][-1].content
    assert response_text, "Agent produced empty response"

    data = _extract_json(response_text)
    assert data is not None, (
        f"Agent response does not contain valid JSON.\n"
        f"Response preview:\n{response_text[:1000]}"
    )

    errors = _validate_xpgraph_json(data)
    assert not errors, (
        f"XpGraph JSON validation failed:\n" + "\n".join(errors)
    )

    # Should have extracted at least some nodes (the user preferences signal is strong)
    assert len(data["nodes"]) > 0, "Should have extracted at least one node"
    print(f"\nExtracted {len(data['nodes'])} nodes, {len(data['edges'])} edges")
    for n in data["nodes"]:
        print(f"  [{n['type']}] {n['name']}: {n['description']}")
    for e in data["edges"]:
        print(f"  EDGE {e['from_name']} → {e['to_name']} [{e['type']}]")


@pytest.mark.asyncio
async def test_nudge_combined_extracts_valid_json():
    """_nudge_combined agent produces valid JSON for XpGraph nodes."""
    agent = await _create_nudge_agent("You are a helpful assistant.")

    res = await agent.ainvoke(input={
        "session_id": "test-nudge-combined",
        "messages": [
            *SAMPLE_MESSAGES,
            HumanMessage(content=_COMBINED_REVIEW_PROMPT),
        ],
    })

    response_text = res["messages"][-1].content
    assert response_text, "Agent produced empty response"

    data = _extract_json(response_text)
    assert data is not None, (
        f"Agent response does not contain valid JSON.\n"
        f"Response preview:\n{response_text[:1000]}"
    )

    errors = _validate_xpgraph_json(data)
    assert not errors, (
        f"XpGraph JSON validation failed:\n" + "\n".join(errors)
    )

    assert len(data["nodes"]) > 0, "Should have extracted at least one node"
    print(f"\n[Combined] Extracted {len(data['nodes'])} nodes, {len(data['edges'])} edges")
    for n in data["nodes"]:
        print(f"  [{n['type']}] {n['name']}: {n['description']}")
    for e in data["edges"]:
        print(f"  EDGE {e['from_name']} → {e['to_name']} [{e['type']}]")


@pytest.mark.asyncio
async def test_nudge_skill_extracts_user_preference_as_skill():
    """User correction (\"don't explain so much\") should produce a SKILL node."""
    agent = await _create_nudge_agent("You are a helpful assistant.")

    res = await agent.ainvoke(input={
        "session_id": "test-nudge-preference",
        "messages": [
            *SAMPLE_MESSAGES,
            HumanMessage(content=_SKILL_REVIEW_PROMPT),
        ],
    })

    response_text = res["messages"][-1].content
    data = _extract_json(response_text)
    assert data is not None, f"No JSON found in response:\n{response_text[:1000]}"

    skill_nodes = [n for n in data["nodes"] if n["type"] == "SKILL"]
    preference_skills = [
        n for n in skill_nodes
        if any(kw in n["name"].lower() for kw in ["concise", "brevity", "terse", "brief", "minimal", "verbosity", "explanation"])
    ]
    assert len(preference_skills) >= 1, (
        f"Expected at least one SKILL node for user preference ('concise responses'). "
        f"Found SKILL nodes: {[n['name'] for n in skill_nodes]}"
    )
    print(f"\nFound preference skill: {preference_skills[0]['name']}")


@pytest.mark.asyncio
async def test_nudge_skill_handles_empty_conversation():
    """Minimal conversation (no notable events) → valid empty JSON."""
    agent = await _create_nudge_agent("You are a helpful assistant.")

    res = await agent.ainvoke(input={
        "session_id": "test-nudge-empty",
        "messages": [
            HumanMessage(content="Hello, how are you?"),
            AIMessage(content="I'm doing great, thanks!"),
            HumanMessage(content=_SKILL_REVIEW_PROMPT),
        ],
    })

    response_text = res["messages"][-1].content
    data = _extract_json(response_text)
    assert data is not None, f"Should return valid JSON even for empty conversations.\nResponse:\n{response_text[:500]}"

    errors = _validate_xpgraph_json(data)
    assert not errors, f"Validation errors:\n" + "\n".join(errors)
    # Empty result is fine
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--no-header"])
