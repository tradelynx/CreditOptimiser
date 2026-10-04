"""Wire CreditOptimiser into Claude Code: cost-aware subagents, the prompt hook and status line.

Changes are planned first and only written with apply=True. settings.json is
backed up before every write, and anything you've already configured is kept.
"""

import json
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .transcripts import default_claude_dir

MARKER = "creditopt"
ROOT = Path(__file__).resolve().parent.parent

AGENTS = {
    "scout": """---
name: scout
description: Cheap, fast codebase explorer. Use PROACTIVELY for finding files, tracing where something is defined or used, and answering "where/what/how is X" questions, so search output stays out of the main context. Returns a concise summary with file:line references.
tools: Read, Grep, Glob
model: haiku
---
You are a codebase scout. Find what was asked using Grep, Glob and Read.
Reply with a short summary and precise file:line references. Don't paste large
blocks of code, and don't suggest changes unless asked. Keep the answer under
300 words.
""",
    "runner": """---
name: runner
description: Runs tests, builds, linters and other long-output commands, then reports only what matters (pass/fail counts, the first real error, file:line). Use PROACTIVELY whenever a command may print a lot.
tools: Bash, Read, Grep
model: haiku
---
Run the command(s) you were given. Report:
1. Pass/fail and counts.
2. For failures, the failing test or step, the key error lines, and file:line.
Never paste full logs. Don't edit files.
""",
    "architect": """---
name: architect
description: Senior design reviewer for architecture decisions, tricky debugging strategy, and plans touching many files. Use when the main session runs on a cheaper model and needs deeper reasoning for one step.
tools: Read, Grep, Glob
model: opus
---
You give a decisive recommendation for the design or debugging question
you're asked. Read only what you need. Return: the recommendation, why,
the risks, and a short ordered plan with file paths. Don't write code.
""",
}


def _python_cmd(sub):
    return (f"PYTHONPATH={shlex.quote(str(ROOT))} {shlex.quote(sys.executable)} "
            f"-m creditopt {sub}")


def _load_settings(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _has_our_hook(settings):
    for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for h in group.get("hooks", []):
            if MARKER in h.get("command", ""):
                return True
    return False


def plan(claude_dir=None, force_statusline=False):
    """List the changes `apply` would make, without touching anything."""
    claude_dir = Path(claude_dir or default_claude_dir())
    settings = _load_settings(claude_dir / "settings.json")
    steps = []
    for name in AGENTS:
        path = claude_dir / "agents" / f"{name}.md"
        steps.append({"what": f"subagent '{name}'", "path": str(path),
                      "action": "skip (exists)" if path.exists() else "create"})
    steps.append({"what": "UserPromptSubmit context guard hook",
                  "path": str(claude_dir / "settings.json"),
                  "action": "skip (installed)" if _has_our_hook(settings) else "add"})
    existing = settings.get("statusLine", {}).get("command", "")
    if MARKER in existing:
        sl = "skip (installed)"
    elif existing and not force_statusline:
        sl = "skip (you already have a status line; use --force-statusline to replace)"
    else:
        sl = "set"
    steps.append({"what": "status line (context meter)", "path": str(claude_dir / "settings.json"),
                  "action": sl})
    return steps


def apply(claude_dir=None, force_statusline=False):
    claude_dir = Path(claude_dir or default_claude_dir())
    steps = plan(claude_dir, force_statusline)
    agents_dir = claude_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name, body in AGENTS.items():
        path = agents_dir / f"{name}.md"
        if not path.exists():
            path.write_text(body)

    settings_path = claude_dir / "settings.json"
    settings = _load_settings(settings_path)
    if settings_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, settings_path.with_name(f"settings.json.{stamp}.bak"))
    if not _has_our_hook(settings):
        settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
            {"hooks": [{"type": "command", "command": _python_cmd("hook"), "timeout": 10}]})
    if steps[-1]["action"] == "set":
        settings["statusLine"] = {"type": "command", "command": _python_cmd("statusline")}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return steps


def uninstall(claude_dir=None):
    """Remove our hook and status line. Subagent files are left for you to delete."""
    claude_dir = Path(claude_dir or default_claude_dir())
    settings_path = claude_dir / "settings.json"
    settings = _load_settings(settings_path)
    groups = settings.get("hooks", {}).get("UserPromptSubmit", [])
    kept = []
    for g in groups:
        g = {**g, "hooks": [h for h in g.get("hooks", []) if MARKER not in h.get("command", "")]}
        if g["hooks"]:
            kept.append(g)
    if "hooks" in settings:
        if kept:
            settings["hooks"]["UserPromptSubmit"] = kept
        else:
            settings["hooks"].pop("UserPromptSubmit", None)
    if MARKER in settings.get("statusLine", {}).get("command", ""):
        settings.pop("statusLine")
    if settings_path.exists():
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return [str(claude_dir / "agents" / f"{n}.md") for n in AGENTS]
