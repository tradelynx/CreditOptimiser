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
            allow = json.loads(f.read_text()).get("permissions", {}).get("allow", [])
        except (OSError, ValueError, AttributeError):
            continue
        if any(str(rule).startswith("Bash") for rule in allow):
            return True
    return False


def _multi_part(task):
    """Count independent parts: bullet/numbered lines, or 'then/also/and also' clauses."""
    bullets = len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S", task))
    return bullets if bullets >= 2 else 0


def plan_run(task, repo, model=None):
    """Decide the lead model and which subagents to give it. Pure: nothing runs."""
    rec = router.route(task)
    lead = model if model in models.BY_KEY else rec["model"]
    m = models.BY_KEY[lead]
    parts = _multi_part(task)
    reasons = set(rec["reasons"])

    shell = shell_allowed(repo)
    agents, why = [], []
    if lead != "haiku":
        agents.append("scout")
        why.append("a Haiku scout explores the codebase so search output stays out of the "
                   f"{m.name} context")
        if shell:
            agents.append("verifier")
            why.append("a Haiku verifier runs the tests cheaply")
    if lead in ("opus", "fable"):
        agents.append("implementer")
        why.append(f"{m.name} leads on design; routine edits go to a Sonnet implementer at "
                   f"{models.BY_KEY['sonnet'].output / m.output:.0%} of the price")
    elif parts >= 3 or "codebase-wide scope" in reasons:
        agents.append("implementer")
        why.append("independent parts are handed to parallel Sonnet implementers")
    if not agents:
        why.append("small task: a single Haiku session is cheapest, so no subagents")

    instructions = [
        "You were launched by CreditOptimiser, which picked your model to balance cost and quality.",
    ]
    if "scout" in agents:
        instructions.append("Delegate searching and exploration to the `scout` subagent instead "
                            "of reading many files yourself.")
    if "implementer" in agents:
        instructions.append("Decide the approach yourself, then delegate clearly specified edits "
                            "to `implementer` subagents (in parallel for independent parts). "
                            "Review their reports before finishing.")
    if "verifier" in agents:
        instructions.append("After changing code, ask the `verifier` subagent to run the relevant "
                            "tests or build. Some commands may still be refused by permissions.")
    else:
        instructions.append("Shell commands are not permitted here, so don't try to run tests or "
                            "builds. Check your work by reading the code, and list the exact "
                            "commands the user should run.")
    instructions.append("End your final message with exactly one line: `STATUS: DONE` if the "
                        "task is complete and verified, `STATUS: UNVERIFIED: <checks to run>` if "
                        "the work is complete but you couldn't run its checks, or "
                        "`STATUS: INCOMPLETE: <reason>` if the work itself isn't finished.")

    # Never auto-escalate to Fable: at 2.5x Opus prices, that should be your call.
    escalate_to = next((x.key for x in models.CATALOGUE if x.tier == m.tier + 1 and x.key != "fable"), None)
    return {
        "task": task,
        "repo": repo,
        "model": lead,
        "model_name": m.name,
        "routed_model": rec["model"],
        "route_reasons": rec["reasons"],
        "agents": [{"name": a, "model": AGENT_DEFS[a]["model"],
                    "description": AGENT_DEFS[a]["description"]} for a in agents],
        "strategy": why,
        "parts": parts,
        "permission_mode": PERMISSION_MODE,
        "shell_allowed": shell,
        "escalate_to": escalate_to if config.load()["auto_escalate"] else None,
        "system_prompt": " ".join(instructions),
    }


def find_claude():
    cfg = config.load()
    candidates = [cfg.get("claude_path"), shutil.which("claude"),
                  str(Path.home() / ".claude" / "local" / "claude"),
                  "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


def build_command(plan, claude, prompt=None, resume=None, model=None):
    cmd = [claude, "-p", prompt or plan["task"],
           "--model", models.BY_KEY[model or plan["model"]].cli_alias,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", plan["permission_mode"],
           "--append-system-prompt", plan["system_prompt"]]
    if plan["agents"]:
        cmd += ["--agents", json.dumps({a["name"]: AGENT_DEFS[a["name"]] for a in plan["agents"]})]
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
        usage = {}
        for mid, u in (d.get("modelUsage") or {}).items():
            usage[models.family(mid)] = usage.get(models.family(mid), 0) + (u.get("costUSD") or 0)
        text = d.get("result") or ""
        m = STATUS_RE.search(text)
        out.append({"kind": "result", "ok": not d.get("is_error"), "subtype": d.get("subtype"),
                    "cost": d.get("total_cost_usd") or 0, "by_model": usage,
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
        self.started = datetime.now(timezone.utc).isoformat()
        self.proc = None
        self._lock = threading.Lock()

    def emit(self, ev):
        with self._lock:
            ev["t"] = time.time()
            self.events.append(ev)

    def snapshot(self, after=0):
        with self._lock:
            return {"id": self.id, "state": self.state, "plan": self.plan, "cost": round(self.cost, 4),
                    "by_model": {k: round(v, 4) for k, v in self.by_model.items()},
                    "started": self.started, "events": self.events[after:], "next": len(self.events)}

    def cancel(self):
        self.state = "cancelled"
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


RUNS = {}


def _attempt(run, cmd):
    """Run one claude process; return its result event (or None)."""
    errfile = tempfile.TemporaryFile(mode="w+")
    run.proc = subprocess.Popen(cmd, cwd=run.plan["repo"], stdout=subprocess.PIPE,
                                stderr=errfile, stdin=subprocess.DEVNULL, text=True, bufsize=1)
    result = None
    for line in run.proc.stdout:
        for ev in parse_event(line):
            if ev["kind"] == "result":
                result = ev
                run.cost += ev["cost"]
                for k, v in ev["by_model"].items():
                    run.by_model[k] = run.by_model.get(k, 0) + v
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
        with open(path, "a") as fh:
            fh.write(json.dumps({"id": run.id, "started": run.started, "state": run.state,
                                 "task": run.plan["task"][:300], "repo": run.plan["repo"],
                                 "model": run.plan["model"], "cost": run.cost,
                                 "by_model": run.by_model}) + "\n")
    except OSError:
        pass


def start(task, repo, model=None):
    """Plan and launch a run in the background. Returns the Run, or raises ValueError."""
    task = (task or "").strip()
    if not task:
        raise ValueError("Describe the task first.")
    if not repo or not Path(repo).is_dir():
        raise ValueError("Pick a repository to run in.")
    claude = find_claude()
    if not claude:
        raise ValueError("Couldn't find the `claude` command. Install Claude Code, or set "
                         "claude_path in settings.")
    run = Run(plan_run(task, repo, model))
    RUNS[run.id] = run
    threading.Thread(target=_execute, args=(run, claude), daemon=True).start()
    return run


def history(limit=20):
    try:
        lines = (config.config_dir() / "runs.jsonl").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out
