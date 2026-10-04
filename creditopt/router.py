"""Pick the cheapest model that's likely to do a task well.

A transparent keyword-and-scope heuristic: it runs offline, costs nothing, and
explains every decision, so you can see why it chose a model and overrule it.
"""

import re

from . import models

# (pattern, weight). Positive pushes toward bigger models, negative toward smaller.
SIGNALS = [
    # Haiku territory: mechanical, lookup or single-shot work
    (r"\b(rename|typo|spelling|reformat|format|lint|prettier|sort imports)\b", -3, "mechanical edit"),
    (r"\b(find|search|grep|locate|where is|list (all|the)|which files?)\b", -2, "search / lookup"),
    (r"\b(what does|explain (this|the) (line|function|error)|quick question|summari[sz]e)\b", -2, "explanation / summary"),
    (r"\b(convert|translate|json to|yaml|csv|regex)\b", -1, "conversion"),
    (r"\b(commit message|changelog|docstring|comment)\b", -2, "boilerplate text"),
    (r"\b(run (the )?tests?|bump|update (the )?version)\b", -2, "routine command"),
    # Sonnet territory: normal engineering
    (r"\b(implement|add|build|create|write)\b", 1, "builds something new"),
    (r"\b(fix|bug|broken|failing|error)\b", 1, "bug fixing"),
    (r"\b(tests?|unit test|coverage)\b", 1, "testing"),
    (r"\b(refactor|clean ?up|extract)\b", 1, "refactoring"),
    (r"\b(endpoint|component|page|form|api|feature)\b", 1, "feature work"),
    # Opus territory: judgement, ambiguity and breadth
    (r"\b(architect|architecture|design|system design|trade-?offs?)\b", 3, "design / architecture"),
    (r"\b(plan|strategy|roadmap|approach|investigate|root cause)\b", 2, "planning / investigation"),
    (r"\b(race condition|deadlock|memory leak|intermittent|flaky|heisenbug|concurren\w+)\b", 3, "hard debugging"),
    (r"\b(security|vulnerab\w+|auth\w*|crypto\w*|threat model)\b", 2, "security-sensitive"),
    (r"\b(migrat\w+|upgrade .* (framework|version)|rewrite)\b", 2, "migration / rewrite"),
    (r"\b(performance|optimi[sz]e|bottleneck|scal\w+)\b", 2, "performance"),
    (r"\b(review|audit)\b", 2, "review / audit"),
    (r"\b(whole|entire|across the|all (the )?(services|modules|packages)|codebase|monorepo)\b", 2, "codebase-wide scope"),
    (r"\b(algorithm|proof|formal|math\w*|novel)\b", 2, "deep reasoning"),
    # Fable territory: escalation
    (r"\b(opus (failed|couldn't|struggled|got stuck)|still (broken|failing) after|research[- ]grade|days? of work)\b", 4, "escalation after a stronger model struggled"),
]

_COMPILED = [(re.compile(p, re.I), w, why) for p, w, why in SIGNALS]


def _scope_signals(text):
    score, reasons = 0, []
    files = len(set(re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|cs|php|kt|swift|c|cpp|h|sql|md|ya?ml|json)\b", text)))
    if files >= 4:
        score += 2
        reasons.append(f"touches {files} files")
    elif files == 1:
        score -= 1
        reasons.append("single file")
    words = len(text.split())
    if words > 120:
        score += 2
        reasons.append("long, detailed brief")
    elif words < 12:
        score -= 1
        reasons.append("short, well-scoped ask")
    if re.search(r"\b(not sure|unclear|somehow|figure out|why (does|is|do))\b", text, re.I):
        score += 1
        reasons.append("open-ended / ambiguous")
    return score, reasons


def route(task):
    """Return a recommendation dict for a free-text task description."""
    text = (task or "").strip()
    score, reasons = 0, []
    for rx, weight, why in _COMPILED:
        if rx.search(text):
            score += weight
            reasons.append(why)
    s, r = _scope_signals(text)
    score += s
    reasons += r

    if score >= 9 and any("escalation" in x for x in reasons):
        key = "fable"
    elif score >= 4:
        key = "opus"
    elif score >= 0:
        key = "sonnet"
    else:
        key = "haiku"

    m = models.BY_KEY[key]
    tips = []
    if key == "opus":
        tips.append("Plan with Opus, then execute with Sonnet: `/model opusplan` uses Opus in plan "
                    "mode and Sonnet for the edits.")
    if key in ("opus", "fable"):
        tips.append("Write the full brief up front (goal, constraints, files, done-criteria). "
                    "Fewer back-and-forth turns means less context re-read.")
    if key in ("haiku", "sonnet"):
        tips.append("Run it in a fresh session (`/clear`) so you don't pay to re-read an unrelated "
                    "conversation.")
    if any(x in reasons for x in ("search / lookup", "codebase-wide scope")):
        tips.append("Delegate exploration to the Haiku `scout` subagent so the search results "
                    "don't bloat your main context.")

    cheaper = next((x for x in models.CATALOGUE if x.tier == m.tier - 1), None)
    return {
        "task": text,
        "model": key,
        "name": m.name,
        "model_id": m.model_id,
        "score": score,
        "reasons": reasons or ["no strong signals, so defaulting to the everyday model"],
        "command": f'claude --model {m.cli_alias} "{_shell_escape(text[:300])}"' if text else "",
        "switch": f"/model {m.cli_alias}",
        "relative_cost": round(m.output / models.BY_KEY["sonnet"].output, 2),
        "fallback": cheaper.name if cheaper else None,
        "tips": tips,
    }


def _shell_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
