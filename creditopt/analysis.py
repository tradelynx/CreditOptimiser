"""Turn parsed transcripts into usage stats and savings recommendations.

Every "saving" here is an estimate in API-equivalent dollars: what the same
work would have cost had it been done the recommended way.
"""

from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import timedelta

from . import models

# Defaults, overridable via Settings.
CONTEXT_BUDGET = 150_000       # past this, compact (every turn re-reads the whole context)
COMPACT_RESIDUE = 20_000       # rough context size right after /compact
CACHE_TTL_1H = timedelta(hours=1)
CACHE_TTL_5M = timedelta(minutes=5)
REBUILD_MIN_TOKENS = 20_000    # ignore small cache rebuilds
LIGHT_OUTPUT_PER_TURN = 1_500  # Opus sessions below this output/turn are "light"
LIGHT_PEAK_CONTEXT = 80_000
LONG_SESSION_TURNS = 30


@dataclass
class Settings:
    context_budget: int = CONTEXT_BUDGET
    compact_residue: int = COMPACT_RESIDUE


def call_cost(c, model_id=None):
    return models.cost(model_id or c.model, c.input_tokens, c.output_tokens, c.cache_read,
                       c.cache_write_5m, c.cache_write_1h)


def _main_model(calls):
    spend = defaultdict(float)
    for c in calls:
        spend[c.model] += call_cost(c)
    return max(spend, key=spend.get) if spend else ""


def _tokens(calls):
    return {
        "input": sum(c.input_tokens for c in calls),
        "output": sum(c.output_tokens for c in calls),
        "cache_read": sum(c.cache_read for c in calls),
        "cache_write": sum(c.cache_write_5m + c.cache_write_1h for c in calls),
    }


def summarise_session(s, settings):
    main = [c for c in s.calls if not c.sidechain]
    peak = max((c.context_tokens for c in main), default=0)
    model_id = _main_model(main or s.calls)
    return {
        "id": s.session_id,
        "project": s.project,
        "title": s.first_prompt or "(no prompt)",
        "start": s.calls[0].timestamp.isoformat(),
        "end": s.calls[-1].timestamp.isoformat(),
        "model": models.family(model_id),
        "model_id": model_id,
        "calls": len(s.calls),
        "subagent_calls": len(s.calls) - len(main),
        "turns": s.user_turns,
        "compactions": s.compactions,
        "peak_context": peak,
        "over_budget_calls": sum(1 for c in main if c.context_tokens > settings.context_budget),
        "cost": round(sum(call_cost(c) for c in s.calls), 4),
        "tokens": _tokens(s.calls),
    }


# --- recommendations ---------------------------------------------------------

def _rec(kind, severity, title, detail, saving, action, sessions=()):
    return {"kind": kind, "severity": severity, "title": title, "detail": detail,
            "saving": round(saving, 2), "action": action, "sessions": list(sessions)[:10]}


def compaction_saving(calls, settings):
    """Replay a session as if it had compacted whenever it passed the budget.

    After each simulated /compact the context drops to the residue and then
    regrows as it really did. The compaction itself is charged too: one read of
    the full context plus writing the summary.
    """
    offset, saving = 0, 0.0
    for c in calls:
        if c.sidechain:
            continue
        m = models.lookup(c.model)
        effective = c.context_tokens - offset
        if effective > settings.context_budget:
            saving -= (effective * m.cache_read + settings.compact_residue * m.output) / 1_000_000
            offset += effective - settings.compact_residue
        saving += offset * m.cache_read / 1_000_000
    return saving


def _context_bloat(sessions, settings):
    saving, hits = 0.0, []
    for s in sessions:
        extra = compaction_saving(s.calls, settings)
        if extra > 0:
            saving += extra
            hits.append(s.session_id)
    if not hits:
        return None
    return _rec(
        "context", "high" if saving > 5 else "medium",
        f"{len(hits)} session(s) ran past {settings.context_budget // 1000}k tokens of context",
        "Every request re-reads the whole conversation. Past the budget, each turn costs far more "
        "than it needs to, and answer quality drifts as old, irrelevant detail piles up.",
        saving,
        "Run `/compact` when the status line turns amber, or `/clear` when you switch tasks. "
        "`creditopt install` adds a hook that warns you automatically.",
        hits)


def _cache_rebuilds(sessions):
    saving, hits = 0.0, []
    for s in sessions:
        prev = None
        for c in s.calls:
            if c.sidechain:
                continue
            if prev is not None:
                gap = c.timestamp - prev.timestamp
                ttl = CACHE_TTL_1H if c.cache_write_1h else CACHE_TTL_5M
                written = c.cache_write_5m + c.cache_write_1h
                if gap > ttl and written >= REBUILD_MIN_TOKENS:
                    m = models.lookup(c.model)
                    rebuild = (c.cache_write_5m * models.CACHE_WRITE_5M
                               + c.cache_write_1h * models.CACHE_WRITE_1H) * m.input
                    saving += (rebuild - written * m.cache_read) / 1_000_000
                    if s.session_id not in hits:
                        hits.append(s.session_id)
            prev = c
    if not hits:
        return None
    return _rec(
        "cache", "medium",
        f"Large contexts were re-cached after idle breaks in {len(hits)} session(s)",
        "The prompt cache expires when a session sits idle. Coming back to a big conversation means "
        "paying to write the entire context to cache again.",
        saving,
        "Before stepping away from a long session, `/compact` it (or `/clear` if the task is done) "
        "so the resume is cheap.",
        hits)


def _overpowered(sessions, settings):
    saving, hits = 0.0, []
    for s in sessions:
        main = [c for c in s.calls if not c.sidechain]
        if not main or models.family(_main_model(main)) not in ("opus", "fable"):
            continue
        out_per_turn = sum(c.output_tokens for c in main) / max(s.user_turns, 1)
        peak = max(c.context_tokens for c in main)
        if out_per_turn < LIGHT_OUTPUT_PER_TURN and peak < LIGHT_PEAK_CONTEXT:
            saving += sum(call_cost(c) - call_cost(c, "claude-sonnet-5-5") for c in main)
            hits.append(s.session_id)
    if not hits:
        return None
    return _rec(
        "model", "medium",
        f"{len(hits)} light session(s) used a top-tier model",
        "Short sessions with small answers and small context rarely need Opus or Fable. "
        "Sonnet handles them at roughly half the cost.",
        saving,
        "Start routine work with `claude --model sonnet` (or `/model sonnet`). Use the Task Router "
        "tab to check before you start.",
        hits)


def _subagents(sessions):
    saving, n = 0.0, 0
    for s in sessions:
        for c in s.calls:
            if c.sidechain and models.family(c.model) in ("opus", "fable", "sonnet"):
                saving += call_cost(c) - call_cost(c, "claude-haiku-4-5")
                n += 1
    if n == 0 or saving < 0.01:
        return None
    return _rec(
        "subagents", "low",
        f"{n} subagent request(s) ran on Sonnet or above",
        "Subagents mostly search and read files. That's Haiku-level work, and Haiku is a fraction "
        "of the price.",
        saving,
        "`creditopt install` adds Haiku-pinned scout/runner subagents, so delegated exploration "
        "and test runs stay cheap.",
        ())


def _long_sessions(sessions):
    hits = [s.session_id for s in sessions
            if s.user_turns >= LONG_SESSION_TURNS and s.compactions == 0]
    if not hits:
        return None
    return _rec(
        "hygiene", "low",
        f"{len(hits)} session(s) went {LONG_SESSION_TURNS}+ turns without a /compact or /clear",
        "Long sessions usually cover several tasks. Earlier tasks' files and tool output keep "
        "getting re-sent long after they stop mattering.",
        0.0,
        "Use `/clear` between unrelated tasks. Put anything worth keeping in CLAUDE.md first.",
        hits)


def recommendations(sessions, settings):
    recs = [r for r in (
        _context_bloat(sessions, settings),
        _cache_rebuilds(sessions),
        _overpowered(sessions, settings),
        _subagents(sessions),
        _long_sessions(sessions),
    ) if r]
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (order[r["severity"]], -r["saving"]))
    return recs


# --- aggregate report --------------------------------------------------------

def _peak_window(calls, hours=5):
    """Most expensive rolling window: subscription limits reset on a 5-hour cycle."""
    best, best_start = 0.0, None
    window, total, j = timedelta(hours=hours), 0.0, 0
    costs = [call_cost(c) for c in calls]
    for i, c in enumerate(calls):
        total += costs[i]
        while c.timestamp - calls[j].timestamp > window:
            total -= costs[j]
            j += 1
        if total > best:
            best, best_start = total, calls[j].timestamp
    return {"cost": round(best, 2), "start": best_start.isoformat() if best_start else None}


def build_report(sessions_by_id, settings=None):
    settings = settings or Settings()
    sessions = sorted(sessions_by_id.values(), key=lambda s: s.calls[0].timestamp)
    calls = sorted((c for s in sessions for c in s.calls), key=lambda c: c.timestamp)

    by_model = defaultdict(lambda: {"cost": 0.0, "calls": 0, "output": 0})
    daily = defaultdict(lambda: defaultdict(float))
    by_project = defaultdict(float)
    for c in calls:
        fam, cost = models.family(c.model), call_cost(c)
        by_model[fam]["cost"] += cost
        by_model[fam]["calls"] += 1
        by_model[fam]["output"] += c.output_tokens
        daily[c.timestamp.date().isoformat()][fam] += cost
        by_project[c.project] += cost

    tok = _tokens(calls)
    read_side = tok["input"] + tok["cache_read"] + tok["cache_write"]
    recs = recommendations(sessions, settings)
    total = sum(v["cost"] for v in by_model.values())

    return {
        "settings": asdict(settings),
        "catalogue": [asdict(m) for m in models.CATALOGUE],
        "totals": {
            "cost": round(total, 2),
            "calls": len(calls),
            "sessions": len(sessions),
            "tokens": tok,
            "cache_hit_rate": round(tok["cache_read"] / read_side, 3) if read_side else 0,
            "potential_saving": round(sum(r["saving"] for r in recs), 2),
            "first": calls[0].timestamp.isoformat() if calls else None,
            "last": calls[-1].timestamp.isoformat() if calls else None,
            "peak_5h_window": _peak_window(calls),
        },
        "by_model": {k: {**v, "cost": round(v["cost"], 4)} for k, v in by_model.items()},
        "daily": [{"date": d, **{k: round(v, 4) for k, v in fams.items()}}
                  for d, fams in sorted(daily.items())],
        "by_project": sorted(({"project": p, "cost": round(v, 4)} for p, v in by_project.items()),
                             key=lambda r: -r["cost"]),
        "sessions": sorted((summarise_session(s, settings) for s in sessions),
                           key=lambda r: r["end"], reverse=True),
        "recommendations": recs,
    }
