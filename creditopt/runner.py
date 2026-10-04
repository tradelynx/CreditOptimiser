"""Run a task with Claude Code on the most cost-effective setup.

The router picks the lead model. The plan then adds subagents where they pay
off: Haiku scouts to keep searching out of the expensive context, a Sonnet
implementer so an Opus lead isn't spending Opus rates on routine edits, and
parallel implementers for tasks with independent parts. Runs use acceptEdits
permissions: files can be edited, shell commands are refused unless the repo's
own Claude Code settings allow them.

Quality guard: Claude ends each run with a STATUS line. If a run errors out or
reports the work itself is unfinished, it is resumed once on the next model up,
keeping the work done so far. Work that's finished but couldn't be tested
(UNVERIFIED) is not escalated: a bigger model can't run the tests either.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, models, router

PERMISSION_MODE = "acceptEdits"
STATUS_RE = re.compile(r"STATUS:\s*(DONE|UNVERIFIED|INCOMPLETE)\b[\s:—-]*(.*)", re.I)

AGENT_DEFS = {
    "scout": {
        "description": "Cheap, fast codebase explorer. Use for finding files, tracing definitions "
                       "and usages, and answering where/what/how questions. Returns a short "
                       "summary with file:line references.",
        "prompt": "You are a codebase scout. Find what was asked using Grep, Glob and Read. "
                  "Reply with a short summary and precise file:line references. Don't paste "
                  "large blocks of code. Stay under 300 words.",
        "tools": ["Read", "Grep", "Glob"],
        "model": "haiku",
    },
    "implementer": {
        "description": "Makes well-specified code changes. Give it the exact files, the change "
                       "to make and the acceptance criteria. Use for routine edits once the "
                       "design is decided, and in parallel for independent parts.",
        "prompt": "You implement precisely specified changes. Make the edits, keep to the "
                  "existing style, and report what you changed as file:line bullets. If the "
                  "spec is ambiguous or wrong, stop and say so instead of guessing.",
        "tools": ["Read", "Grep", "Glob", "Edit", "Write"],
        "model": "sonnet",
    },
    "reviewer": {
        "description": "Independent senior reviewer. Use once the work looks finished: it reads the "
                       "diff against the original task and reports real problems (bugs, missed "
                       "requirements, broken edge cases), not style nits.",
        "prompt": "You review a change against the task you're given. Run `git diff` and "
                  "`git status`, read whatever surrounding code you need, and list concrete "
                  "problems with file:line and why each matters. If it's correct and complete, say "
                  "so plainly. Don't edit files.",
        "tools": ["Bash", "Read", "Grep", "Glob"],
        "model": "opus",
    },
    "verifier": {
        "description": "Runs tests, builds or linters and reports only what matters. Use after "
                       "making changes, if shell commands are permitted in this repo.",
        "prompt": "Run the given command(s). Report pass/fail with counts and, for failures, "
                  "the key error lines with file:line. Never paste full logs. Don't edit files.",
        "tools": ["Bash", "Read", "Grep"],
        "model": "haiku",
    },
}


def shell_allowed(repo, claude_dir=None):
    """True if Claude Code settings let this repo run any shell command unprompted."""
    from .transcripts import default_claude_dir
    files = [Path(claude_dir or default_claude_dir()) / "settings.json"]
    if repo:
        files += [Path(repo) / ".claude" / "settings.json", Path(repo) / ".claude" / "settings.local.json"]
    for f in files:
        try:
            allow = json.loads(f.read_text(encoding="utf-8")).get("permissions", {}).get("allow", [])
        except (OSError, ValueError, AttributeError):
            continue
        if any(str(rule).startswith("Bash") for rule in allow):
            return True
    return False


def _multi_part(task):
    """Count independent parts: bullet or numbered lines."""
    bullets = len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S", task))
    return bullets if bullets >= 2 else 0


# --- run options ---------------------------------------------------------------
#
# Every behaviour is a setting. A preset fills them all in; any single setting
# can then be changed. "quality" is the default: CreditOptimiser still routes
# to the cheapest suitable model, but it checks and reviews the work.

PRESETS = {
    "savings": {"priority": "savings", "model": "auto", "subagents": "auto", "self_test": False,
                "review": False, "escalate": "off", "access": "edit"},
    "balanced": {"priority": "balanced", "model": "auto", "subagents": "auto", "self_test": True,
                 "review": False, "escalate": "opus", "access": "edit"},
    "quality": {"priority": "quality", "model": "auto", "subagents": "auto", "self_test": True,
                "review": True, "escalate": "opus", "access": "edit"},
}
OPTION_KEYS = list(PRESETS["quality"]) + ["test_command"]
CHOICES = {
    "priority": ("savings", "balanced", "quality"),
    "model": ("auto", "haiku", "sonnet", "opus", "fable"),
    "escalate": ("off", "sonnet", "opus", "fable"),
    "access": ("plan", "edit"),
}
SUBAGENTS = ("scout", "implementer", "verifier", "reviewer")


def resolve_options(options=None):
    """Merge a preset, the user's saved defaults and per-run overrides into one dict."""
    options = dict(options or {})
    saved = config.load().get("run_defaults") or {}
    preset = options.get("preset") or saved.get("preset") or "quality"
    if preset not in PRESETS:
        preset = "quality"
    out = {**PRESETS[preset], "test_command": ""}
    if not options.get("preset") or options.get("preset") == saved.get("preset"):
        out.update({k: v for k, v in saved.items() if k in OPTION_KEYS})
    out.update({k: v for k, v in options.items() if k in OPTION_KEYS and v is not None})
    for k, allowed in CHOICES.items():
        if out[k] not in allowed:
            out[k] = PRESETS[preset][k]
    sa = out["subagents"]
    if isinstance(sa, str) and sa not in ("auto", "off"):
        sa = [x.strip() for x in sa.split(",")]
    if isinstance(sa, list):
        sa = [x for x in sa if x in SUBAGENTS]
    out["subagents"] = sa
    out["self_test"] = bool(out["self_test"])
    out["review"] = bool(out["review"])
    out["test_command"] = str(out.get("test_command") or "").strip()
    out["preset"] = preset
    return out


def detect_test_command(repo):
    """Best guess at a repo's test command, or '' if there's no obvious one."""
    root = Path(repo or ".")
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        script = (pkg.get("scripts") or {}).get("test", "")
        if script and "no test specified" not in script:
            if (root / "pnpm-lock.yaml").exists():
                return "pnpm test"
            if (root / "yarn.lock").exists():
                return "yarn test"
            return "npm test"
    except (OSError, ValueError):
        pass
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    py = any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"))
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return f"{config.python_command()} -m pytest"
    try:
        if "pytest" in (root / "pyproject.toml").read_text(encoding="utf-8"):
            return f"{config.python_command()} -m pytest"
    except OSError:
        pass
    if (root / "tests").is_dir() and (py or any((root / "tests").glob("test_*.py"))):
        return f"{config.python_command()} -m unittest discover -s tests"
    try:
        if re.search(r"(?m)^test:", (root / "Makefile").read_text(encoding="utf-8")):
            return "make test"
    except OSError:
        pass
    if (root / "Gemfile").exists() and (root / "spec").is_dir():
        return "bundle exec rspec"
    return ""


def allowed_tools(plan):
    """Extra permissions granted for this run only (never written to settings)."""
    tools = []
    if plan["test_command"]:
        tools += [f"Bash({plan['test_command']})", f"Bash({plan['test_command']} *)"]
    if any(a["name"] == "reviewer" for a in plan["agents"]):
        tools += ["Bash(git diff)", "Bash(git diff *)", "Bash(git status)", "Bash(git status *)"]
    return tools


def plan_run(task, repo, model=None, options=None):
    """Decide the lead model, sub-agents and checks. Pure: nothing runs."""
    opts = resolve_options({**(options or {}), **({"model": model} if model else {})})
    rec = router.route(task, opts["priority"])
    lead = opts["model"] if opts["model"] != "auto" else rec["model"]
    m = models.BY_KEY[lead]
    parts = _multi_part(task)
    reasons = set(rec["reasons"])
    plan_only = opts["access"] == "plan"

    test_cmd = ""
    if opts["self_test"] and not plan_only:
        test_cmd = opts["test_command"] or detect_test_command(repo)
    shell = shell_allowed(repo) or bool(test_cmd)

    # Which sub-agents: chosen automatically, switched off, or picked by hand.
    if opts["subagents"] == "off":
        agents = []
    elif isinstance(opts["subagents"], list):
        agents = list(opts["subagents"])
    else:
        agents = []
        if lead != "haiku":
            agents.append("scout")
        if lead in ("opus", "fable") and not plan_only:
            agents.append("implementer")
        elif (parts >= 3 or "codebase-wide scope" in reasons) and not plan_only:
            agents.append("implementer")
        if lead != "haiku" and shell and not plan_only:
            agents.append("verifier")
    review_skipped = ""
    if opts["review"] and not plan_only and "reviewer" not in agents:
        if lead == "haiku":
            review_skipped = "no Opus review: for a small mechanical task it would cost more than the task"
        elif lead == "fable":
            review_skipped = "no Opus review: Fable is already the strongest model"
        else:
            agents.append("reviewer")
    if plan_only:
        agents = [a for a in agents if a == "scout"]
    agents = [a for a in SUBAGENTS if a in agents]  # stable order

    why = []
    if "scout" in agents:
        why.append(f"a Haiku scout does the searching, so file dumps stay out of the {m.name} context")
    if "implementer" in agents:
        why.append(f"{m.name} decides the approach; clearly specified edits go to Sonnet implementers"
                   + (" in parallel" if parts >= 3 else ""))
    if "verifier" in agents:
        why.append("a Haiku verifier runs the tests cheaply")
    elif test_cmd:
        why.append(f"the lead runs `{test_cmd}` itself to check its work")
    if "reviewer" in agents:
        why.append(f"an Opus reviewer checks the final diff against the task before it's called done")
    if review_skipped:
        why.append(review_skipped)
    if not agents:
        why.append("no sub-agents: a single session is cheapest for this")
    if opts["self_test"] and not test_cmd and not plan_only:
        why.append("testing is on, but no test command was found. Set one, or it will ask you to test")

    instructions = ["You were launched by CreditOptimiser, which picked your model and helpers to "
                    "get the best result for the credit spent. Don't cut corners to save tokens: "
                    "do the task properly."]
    if plan_only:
        instructions.append("Read-only run: investigate and produce a clear plan. Don't change files.")
    if "scout" in agents:
        instructions.append("Delegate searching and exploration to the `scout` subagent instead of "
                            "reading many files yourself.")
    if "implementer" in agents:
        instructions.append("Decide the approach yourself, then delegate clearly specified edits to "
                            "`implementer` subagents (in parallel for independent parts). Review "
                            "their reports before finishing.")
    if test_cmd:
        runner_name = "the `verifier` subagent" if "verifier" in agents else "yourself"
        instructions.append(f"After changing code, run `{test_cmd}` ({runner_name}) and fix failures "
                            "your change caused. Report any failures that were already there.")
    elif not plan_only:
        instructions.append("Don't try to run tests or builds: shell commands aren't permitted. Check "
                            "your work by reading it, and list the commands the user should run.")
    if "reviewer" in agents:
        instructions.append("When you think you're done, ask the `reviewer` subagent to review the "
                            "changes (it can run git diff) against the original task. Fix every real "
                            "problem it finds before finishing.")
    instructions.append("End your final message with exactly one line: `STATUS: DONE` if the task "
                        "is complete and verified, `STATUS: UNVERIFIED: <checks to run>` if it's "
                        "complete but couldn't be checked, or `STATUS: INCOMPLETE: <reason>` if the "
                        "work itself isn't finished.")

    ceiling = models.BY_KEY[opts["escalate"]].tier if opts["escalate"] != "off" else 0
    escalate_to = next((x.key for x in models.CATALOGUE
                        if x.tier == m.tier + 1 and x.tier <= ceiling), None)
    if not config.load()["auto_escalate"]:
        escalate_to = None
    return {
        "task": task,
        "repo": repo,
        "options": opts,
        "model": lead,
        "model_name": m.name,
        "routed_model": rec["model"],
        "route_reasons": rec["reasons"],
        "agents": [{"name": a, "model": AGENT_DEFS[a]["model"],
                    "description": AGENT_DEFS[a]["description"]} for a in agents],
        "strategy": why,
        "parts": parts,
        "permission_mode": "plan" if plan_only else PERMISSION_MODE,
        "shell_allowed": shell,
        "test_command": test_cmd,
        "detected_test_command": detect_test_command(repo),
        "escalate_to": escalate_to,
        "system_prompt": " ".join(instructions),
    }


def find_claude():
    """Locate the claude command for whichever way Claude Code was installed."""
    cfg = config.load()
    home = Path.home()
    candidates = [cfg.get("claude_path"), shutil.which("claude")]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        candidates += [str(home / ".local" / "bin" / "claude.exe"),
                       str(appdata / "npm" / "claude.cmd")]
    else:
        candidates += [str(home / ".local" / "bin" / "claude"),
                       str(home / ".claude" / "local" / "claude"),
                       "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude"]
    for c in candidates:
        if c and Path(c).is_file() and (os.name == "nt" or os.access(c, os.X_OK)):
            return c
    return None


def launch_prefix(claude):
    """argv prefix that starts Claude Code without going through a shell.

    On Windows an npm install gives a claude.cmd shim. Batch files re-parse
    their arguments through cmd.exe, which would mangle task text containing
    quotes, & or |, so we call node with Claude Code's cli.js directly instead.
    """
    p = Path(claude)
    if p.suffix.lower() == ".py":
        return [sys.executable, claude]
    if p.suffix.lower() in (".cmd", ".bat"):
        pkg = p.parent / "node_modules" / "@anthropic-ai" / "claude-code"
        if (pkg / "claude.exe").is_file():  # newer npm installs ship a native binary
            return [str(pkg / "claude.exe")]
        cli = pkg / "cli.js"
        node = shutil.which("node") or str(p.parent / "node.exe")
        if cli.is_file() and Path(node).is_file():
            return [node, str(cli)]
    return [claude]


def build_command(plan, claude, prompt=None, resume=None, model=None):
    cmd = [*launch_prefix(claude), "-p", prompt or plan["task"],
           "--model", models.BY_KEY[model or plan["model"]].cli_alias,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", plan["permission_mode"],
           "--append-system-prompt", plan["system_prompt"]]
    if plan["agents"]:
        cmd += ["--agents", json.dumps({a["name"]: AGENT_DEFS[a["name"]] for a in plan["agents"]})]
    extra = allowed_tools(plan)
    if extra:
        cmd += ["--allowedTools", *extra]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def parse_event(line):
    """Turn one stream-json line into zero or more display events."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    kind = d.get("type")
    out = []
    if kind == "system" and d.get("subtype") == "init":
        out.append({"kind": "start", "model": d.get("model"), "session": d.get("session_id")})
    elif kind == "assistant":
        sub = bool(d.get("parent_tool_use_id"))
        u = (d.get("message") or {}).get("usage")
        if u and not sub:
            out.append({"kind": "usage", "context": (u.get("input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0)})
        for block in (d.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"kind": "text", "text": block["text"], "sub": sub})
            elif block.get("type") == "tool_use":
                name, inp = block.get("name", ""), block.get("input") or {}
                if name in ("Agent", "Task"):
                    out.append({"kind": "agent", "agent": inp.get("subagent_type", "agent"),
                                "text": inp.get("description") or inp.get("prompt", "")[:140]})
                else:
                    target = inp.get("file_path") or inp.get("pattern") or inp.get("command") or ""
                    out.append({"kind": "tool", "tool": name, "text": str(target)[:160], "sub": sub})
    elif kind == "result":
        usage, tokens = {}, {}
        for mid, u in (d.get("modelUsage") or {}).items():
            fam = models.family(mid)
            usage[fam] = usage.get(fam, 0) + (u.get("costUSD") or 0)
            t = tokens.setdefault(fam, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            t["input"] += u.get("inputTokens") or 0
            t["output"] += u.get("outputTokens") or 0
            t["cache_read"] += u.get("cacheReadInputTokens") or 0
            t["cache_write"] += u.get("cacheCreationInputTokens") or 0
        text = d.get("result") or ""
        m = STATUS_RE.search(text)
        out.append({"kind": "result", "ok": not d.get("is_error"), "subtype": d.get("subtype"),
                    "cost": d.get("total_cost_usd") or 0, "by_model": usage, "tokens": tokens,
                    "turns": d.get("num_turns"), "session": d.get("session_id"),
                    "status": (m.group(1).upper() if m else None),
                    "reason": (m.group(2).strip() if m else ""), "text": text})
    return out


class Run:
    def __init__(self, plan):
        self.id = uuid.uuid4().hex[:12]
        self.plan = plan
        self.events = []
        self.state = "running"   # running | done | unverified | incomplete | failed | cancelled
        self.cost = 0.0
        self.by_model = {}
        self.tokens = {}         # family -> token counts, summed over attempts
        self.peak_context = 0    # largest main-thread context, to judge feasibility
        self.started = datetime.now(timezone.utc).isoformat()
        self.proc = None
        self._lock = threading.Lock()

    def emit(self, ev):
        with self._lock:
            ev["t"] = time.time()
            self.events.append(ev)

    def snapshot(self, after=0):
        with self._lock:
            finished = self.state != "running"
            return {"id": self.id, "state": self.state, "plan": self.plan, "cost": round(self.cost, 4),
                    "comparison": compare(self) if finished and self.tokens else None,
                    "by_model": {k: round(v, 4) for k, v in self.by_model.items()},
                    "started": self.started, "events": self.events[after:], "next": len(self.events)}

    def cancel(self):
        self.state = "cancelled"
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


RUNS = {}


def _price_all(tokens, family):
    """Cost of the given token volumes if every one had gone through `family`."""
    m = models.BY_KEY[family]
    total = {f: sum(t[f] for t in tokens.values()) for f in ("input", "output", "cache_read", "cache_write")}
    # Claude Code writes its prompt cache with the 1-hour TTL (2x input price).
    return models.cost(m.model_id, total["input"], total["output"], total["cache_read"], 0, total["cache_write"])


def compare(run):
    """What this run cost, against the same work priced on each single model.

    The alternatives reprice the run's real token volumes. That is the fairest
    comparison available, but it's an estimate: a different model would have
    produced a somewhat different amount of work.
    """
    lead = run.plan["model"]
    lead_tier = models.BY_KEY[lead].tier
    rows = [{"key": "optimiser", "label": "CreditOptimiser (actual)", "cost": round(run.cost, 4),
             "by_model": {k: round(v, 4) for k, v in run.by_model.items()}, "actual": True, "note": ""}]
    for m in models.CATALOGUE:
        note = ""
        feasible = True
        if run.peak_context > m.context:
            feasible = False
            note = f"not possible: the task needed {run.peak_context // 1000}k tokens of context, over {m.name}'s {m.context // 1000}k"
        elif m.tier < lead_tier:
            note = f"likely lower quality: this task was judged to need {models.BY_KEY[lead].name}"
        rows.append({"key": m.key, "label": f"All on {m.name}", "cost": round(_price_all(run.tokens, m.key), 4),
                     "actual": False, "feasible": feasible, "note": note})
    opus = next(r for r in rows if r["key"] == "opus")["cost"]
    return {"rows": rows, "actual": round(run.cost, 4), "all_opus": opus,
            "saved_vs_opus": round(opus - run.cost, 4),
            "saved_pct": round(100 * (opus - run.cost) / opus, 1) if opus else 0}


def _attempt(run, cmd):
    """Run one claude process; return its result event (or None)."""
    errfile = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    run.proc = subprocess.Popen(cmd, cwd=run.plan["repo"], stdout=subprocess.PIPE,
                                stderr=errfile, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                encoding="utf-8", errors="replace")
    result = None
    for line in run.proc.stdout:
        for ev in parse_event(line):
            if ev["kind"] == "usage":
                run.peak_context = max(run.peak_context, ev["context"])
                continue
            if ev["kind"] == "result":
                result = ev
                run.cost += ev["cost"]
                for k, v in ev["by_model"].items():
                    run.by_model[k] = run.by_model.get(k, 0) + v
                for k, t in ev.pop("tokens").items():
                    acc = run.tokens.setdefault(k, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
                    for f, n in t.items():
                        acc[f] += n
            run.emit(ev)
    run.proc.wait()
    run.proc.stdout.close()
    errfile.seek(0)
    err = errfile.read().strip()[-800:]
    errfile.close()
    if result is None and run.state != "cancelled":
        run.emit({"kind": "error", "text": err or f"claude exited with code {run.proc.returncode}"})
    return result


def _execute(run, claude):
    plan = run.plan
    try:
        result = _attempt(run, build_command(plan, claude))
        needs_more = result is None or not result["ok"] or result["status"] == "INCOMPLETE"
        if needs_more and plan["escalate_to"] and run.state != "cancelled":
            nxt = models.BY_KEY[plan["escalate_to"]]
            reason = (result or {}).get("reason") or "the run ended with an error"
            run.emit({"kind": "escalate", "text": f"Not finished ({reason}). Resuming on {nxt.name}."})
            prompt = (f"A cheaper model started this task and didn't finish: {reason}. "
                      "Review what has been done so far, then complete the task.")
            session = (result or {}).get("session")
            result = _attempt(run, build_command(plan, claude, prompt, resume=session, model=nxt.key))
        if run.state != "cancelled":
            if result is None or not result["ok"]:
                run.state = "failed"
            else:
                run.state = {"INCOMPLETE": "incomplete", "UNVERIFIED": "unverified"}.get(
                    result["status"], "done")
    except Exception as e:  # surface, don't crash the server thread
        run.emit({"kind": "error", "text": str(e)})
        run.state = "failed"
    _log(run)


def _log(run):
    try:
        path = config.config_dir() / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": run.id, "started": run.started, "state": run.state,
                                 "task": run.plan["task"][:300], "repo": run.plan["repo"],
                                 "model": run.plan["model"], "cost": run.cost,
                                 "by_model": run.by_model,
                                 "all_opus": compare(run)["all_opus"] if run.tokens else None}) + "\n")
    except OSError:
        pass


def start(task, repo, model=None, options=None):
    """Plan and launch a run in the background. Returns the Run, or raises ValueError."""
    task = (task or "").strip()
    if not task:
        raise ValueError("Describe the task first.")
    if not repo or not Path(repo).is_dir():
        raise ValueError("Pick a repository to run in.")
    claude = find_claude()
    if not claude:
        raise ValueError("Couldn't find the `claude` command. Install Claude Code, or tell "
                         "CreditOptimiser where it is: "
                         f"{config.python_command()} -m creditopt config --set claude_path=/full/path/to/claude")
    run = Run(plan_run(task, repo, model, options))
    RUNS[run.id] = run
    threading.Thread(target=_execute, args=(run, claude), daemon=True).start()
    return run


def active_runs():
    """Runs still in progress, newest first, so a reloaded page can reconnect."""
    live = [r for r in RUNS.values() if r.state == "running"]
    live.sort(key=lambda r: r.started, reverse=True)
    return [{"id": r.id, "started": r.started, "state": r.state, "task": r.plan["task"][:300],
             "repo": r.plan["repo"], "model": r.plan["model"], "cost": r.cost} for r in live]


def savings_summary():
    """Total saved by logged runs, compared with running each one all on Opus."""
    try:
        lines = (config.config_dir() / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    runs = actual = opus = 0
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("all_opus") is not None:
            runs += 1
            actual += r.get("cost") or 0
            opus += r["all_opus"]
    return {"runs": runs, "actual": round(actual, 2), "all_opus": round(opus, 2),
            "saved": round(opus - actual, 2)}


def history(limit=20):
    try:
        lines = (config.config_dir() / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out
