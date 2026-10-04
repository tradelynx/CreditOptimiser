"""Claude Code integrations: the UserPromptSubmit guard and the status line.

Both read the JSON Claude Code pipes to stdin and must never crash or slow the
session down, so every failure path exits quietly.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, models, router
from .transcripts import read_jsonl, _call_from


def _session_state(transcript_path):
    """Latest main-thread context size, model and timestamp for a transcript."""
    ctx, model, last = 0, "", None
    for entry in read_jsonl(transcript_path):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        call = _call_from(entry, "")
        if call:
            ctx, model, last = call.context_tokens, call.model, call.timestamp
    return ctx, model, last


def _state_file(session_id):
    return config.config_dir() / "state" / f"{session_id}.json"


def _load_state(session_id):
    try:
        return json.loads(_state_file(session_id).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(session_id, state):
    try:
        path = _state_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass


def evaluate_prompt(payload, cfg=None, now=None):
    """Decide what (if anything) to tell the user before this prompt runs.

    Returns the hook's JSON output as a dict, or None for silence.
    """
    cfg = cfg or config.load()
    now = now or datetime.now(timezone.utc)
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).exists():
        return None
    session_id = payload.get("session_id") or Path(transcript).stem
    ctx, model, last = _session_state(transcript)
    state = _load_state(session_id)
    budget = cfg["context_budget"]
    messages = []
    block = False

    level = "ok"
    if ctx >= budget:
        level = "over"
    elif ctx >= budget * cfg["warn_ratio"]:
        level = "warn"

    if level == "over":
        messages.append(f"Context is {ctx // 1000}k tokens, past your {budget // 1000}k budget. "
                        "Every turn now re-reads all of it. Run /compact (or /clear if you're "
                        "switching tasks).")
        block = cfg["block_over_budget"] and not state.get("blocked_once")
        if block:
            state["blocked_once"] = True
    elif level == "warn" and state.get("warned") != "warn":
        messages.append(f"Context at {ctx // 1000}k of your {budget // 1000}k budget. Consider "
                        "/compact soon, or /clear if this is a new task.")
    if level != "ok":
        state["warned"] = level
    else:
        state.pop("warned", None)
        state.pop("blocked_once", None)

    if last and ctx >= 30_000 and (now - last).total_seconds() > cfg["idle_minutes"] * 60:
        messages.append(f"This session sat idle for over {cfg['idle_minutes']} min, so its "
                        f"{ctx // 1000}k-token context will be re-cached at full price. "
                        "If the old context isn't needed, /clear is cheaper.")

    fam = models.family(model)
    if cfg["route_nudges"] and fam in ("opus", "fable") and not state.get("nudged"):
        rec = router.route(payload.get("prompt", ""))
        if rec["model"] == "haiku" and rec["score"] <= -3:
            messages.append(f"This looks like {rec['name']} work ({', '.join(rec['reasons'][:2])}). "
                            f"`{rec['switch']}` would cost a fraction of {models.BY_KEY[fam].name}.")
            state["nudged"] = True

    _save_state(session_id, state)
    if not messages:
        return None
    text = "CreditOptimiser: " + " ".join(messages)
    if block:
        return {"decision": "block",
                "reason": text + " (Prompt held once. Send it again to go ahead anyway.)"}
    return {"systemMessage": text}


def run_hook():
    try:
        payload = json.load(sys.stdin)
        out = evaluate_prompt(payload)
        if out:
            print(json.dumps(out))
    except Exception:  # never break the user's session
        pass
    return 0


def _bar(frac, width=10):
    frac = max(0.0, min(frac, 1.0))
    full = int(frac * width)
    return "█" * full + "░" * (width - full)


def status_line(payload, cfg=None):
    cfg = cfg or config.load()
    transcript = payload.get("transcript_path") or ""
    ctx, model, _ = _session_state(transcript) if Path(transcript).exists() else (0, "", None)
    name = (payload.get("model") or {}).get("display_name") or models.lookup(model).name
    budget = cfg["context_budget"]
    frac = ctx / budget if budget else 0
    if frac >= 1:
        colour, hint = "\033[31m", " → /compact"
    elif frac >= cfg["warn_ratio"]:
        colour, hint = "\033[33m", " → compact soon"
    else:
        colour, hint = "\033[32m", ""
    parts = [name, f"{colour}ctx {_bar(frac)} {ctx // 1000}k/{budget // 1000}k{hint}\033[0m"]
    cost = (payload.get("cost") or {}).get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"≈${cost:.2f} API-equiv")
    return " · ".join(parts)


def run_status_line():
    try:
        print(status_line(json.load(sys.stdin)))
    except Exception:
        print("CreditOptimiser")
    return 0
