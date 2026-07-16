import re
import json
from typing import Any, Dict, List, Optional, Set

from context_engine.curator.helpers import _needle_in_path_component


def _classify_removed_skills(
    removed: List[str],
    added: List[str],
    after_names: Set[str],
    tool_calls: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    consolidated: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []

    parsed_calls: List[Dict[str, Any]] = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict) or tc.get("name") != "skill_manage":
            continue
        raw = tc.get("arguments") or ""
        args: Dict[str, Any] = {}
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                args = json.loads(raw)
            except Exception:
                args = {"_raw": raw}
        if isinstance(args, dict):
            parsed_calls.append(args)

    destinations = set(after_names) | set(added or [])

    for name in removed:
        if not name:
            continue
        into: Optional[str] = None
        evidence: Optional[str] = None
        needles = {name, name.replace("-", "_"), name.replace("_", "-")}

        for args in parsed_calls:
            target = args.get("name")
            if not isinstance(target, str) or not target or target == name:
                continue
            if target not in destinations:
                continue
            haystacks: List[tuple[str, str]] = []
            for key in ("file_path", "file_content", "content", "new_string", "_raw"):
                v = args.get(key)
                if isinstance(v, str):
                    haystacks.append((key, v))
            hit = False
            for key, hay in haystacks:
                for needle in needles:
                    if not needle:
                        continue
                    if key == "file_path":
                        matched = _needle_in_path_component(needle, hay)
                    else:
                        matched = bool(re.search(rf'\b{re.escape(needle)}\b', hay))
                    if matched:
                        hit = True
                        evidence = f"skill_manage action={args.get('action', '?')} on '{target}' referenced '{name}' in {hay[:80]}"
                        break
                if hit:
                    break
            if hit:
                into = target
                break

        if into:
            consolidated.append({"name": name, "into": into, "evidence": evidence})
        else:
            pruned.append({"name": name})

    return {"consolidated": consolidated, "pruned": pruned}


def _parse_structured_summary(llm_final: str) -> Dict[str, List[Dict[str, str]]]:
    empty: Dict[str, List[Dict[str, str]]] = {"consolidations": [], "prunings": []}
    if not llm_final or not isinstance(llm_final, str):
        return empty
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", llm_final, re.DOTALL | re.IGNORECASE)
    if not match:
        return empty
    try:
        import yaml
        data = yaml.safe_load(match.group(1))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty

    out: Dict[str, List[Dict[str, str]]] = {"consolidations": [], "prunings": []}
    for entry in data.get("consolidations") or []:
        if not isinstance(entry, dict):
            continue
        frm, into = entry.get("from"), entry.get("into")
        if isinstance(frm, str) and frm.strip() and isinstance(into, str) and into.strip():
            reason = entry.get("reason")
            out["consolidations"].append({"from": frm.strip(), "into": into.strip(), "reason": (reason or "").strip() if isinstance(reason, str) else ""})
    for entry in data.get("prunings") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            reason = entry.get("reason")
            out["prunings"].append({"name": name.strip(), "reason": (reason or "").strip() if isinstance(reason, str) else ""})
    return out


def _extract_absorbed_into_declarations(tool_calls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for tc in tool_calls or []:
        if not isinstance(tc, dict) or tc.get("name") != "skill_manage":
            continue
        raw = tc.get("arguments") or ""
        args: Dict[str, Any] = {}
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                args = json.loads(raw)
            except Exception:
                continue
        if not isinstance(args, dict) or args.get("action") != "delete":
            continue
        name = args.get("name")
        if not isinstance(name, str) or not name.strip() or "absorbed_into" not in args:
            continue
        target = args.get("absorbed_into")
        if target is None or not isinstance(target, str):
            continue
        out[name.strip()] = {"into": target.strip(), "declared": True}
    return out


def _reconcile_classification(
    removed: List[str],
    heuristic: Dict[str, List[Dict[str, Any]]],
    model_block: Dict[str, List[Dict[str, str]]],
    destinations: Set[str],
    absorbed_declarations: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    heur_cons = {e["name"]: e for e in heuristic.get("consolidated", [])}
    model_cons = {e["from"]: e for e in model_block.get("consolidations", [])}
    model_pruned = {e["name"]: e for e in model_block.get("prunings", [])}
    declared = absorbed_declarations or {}

    consolidated: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []

    for name in removed:
        mc = model_cons.get(name)
        mp = model_pruned.get(name)
        hc = heur_cons.get(name)
        dec = declared.get(name)

        if dec is not None:
            into_claim = dec.get("into", "")
            if into_claim and into_claim in destinations:
                entry: Dict[str, Any] = {"name": name, "into": into_claim, "source": "absorbed_into (model-declared at delete)", "reason": (mc.get("reason") or "") if mc else ""}
                if hc and hc.get("evidence"):
                    entry["evidence"] = hc["evidence"]
                consolidated.append(entry)
                continue
            if into_claim == "":
                pruned.append({"name": name, "source": "absorbed_into=\"\" (model-declared prune)", "reason": (mp.get("reason") or "") if mp else ""})
                continue

        if mc and mc.get("into") in destinations:
            entry = {"name": name, "into": mc["into"], "source": "model" + ("+audit" if hc else ""), "reason": mc.get("reason") or ""}
            if hc and hc.get("evidence"):
                entry["evidence"] = hc["evidence"]
            consolidated.append(entry)
            continue

        if mc and mc.get("into") not in destinations:
            if hc:
                consolidated.append({"name": name, "into": hc["into"], "source": "tool-call audit (model named missing umbrella)", "reason": "", "evidence": hc.get("evidence", ""), "model_claimed_into": mc["into"]})
            else:
                pruned.append({"name": name, "source": "fallback (model named missing umbrella, no tool-call evidence)", "reason": ""})
            continue

        if hc:
            consolidated.append({"name": name, "into": hc["into"], "source": "tool-call audit (model omitted from structured block)", "reason": "", "evidence": hc.get("evidence", "")})
            continue

        reason = mp.get("reason", "") if mp else ""
        pruned.append({"name": name, "source": "model" if mp else "no-evidence fallback", "reason": reason})

    return {"consolidated": consolidated, "pruned": pruned}
