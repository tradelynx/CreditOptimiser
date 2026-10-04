"""Wire CreditOptimiser into Claude Code: cost-aware subagents, the prompt hook and status line.

Installs for all of Claude Code (~/.claude) or, with repo=..., for one repository only.

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


def _python_exe():
    """A Python program name that runs in bash, zsh, Git Bash and PowerShell.

    On Windows, Claude Code runs hooks through Git Bash if it's installed and
    PowerShell otherwise. PowerShell won't run a quoted program path, so the
    program must be unquoted: use the full path when it has no spaces, else
    the `py` launcher or `python` from PATH.
    """
    exe = Path(sys.executable).as_posix()
    if " " not in exe:
        return exe
    for name in (("py", "-3"), ("python",), ("python3",)):
        if shutil.which(name[0]):
            return " ".join(name)
    return shlex.quote(exe)  # last resort: works in bash/zsh only


def _python_cmd(sub):
    """Command Claude Code runs for the hook / status line, on any OS."""
    launcher = (ROOT / "run_creditopt.py").as_posix()
    return f'{_python_exe()} "{launcher}" {sub}'



def _load_settings(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _has_our_hook(settings):
    for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
        for h in group.get("hooks", []):
            if MARKER in h.get("command", ""):
                return True
    return False


def targets(claude_dir=None, repo=None):
    """Where to write: (settings file, agents folder).

    With a repo, everything goes in that repo's .claude/ folder, so it only
    applies when Claude Code runs there. The hook lives in settings.local.json
    (personal, not committed) because its command contains local paths.
    """
    if repo:
        base = Path(repo).expanduser().resolve() / ".claude"
        return base / "settings.local.json", base / "agents"
    base = Path(claude_dir or default_claude_dir())
    return base / "settings.json", base / "agents"


def plan(claude_dir=None, force_statusline=False, repo=None):
    """List the changes `apply` would make, without touching anything."""
    settings_path, agents_dir = targets(claude_dir, repo)
    settings = _load_settings(settings_path)
    steps = []
    for name in AGENTS:
        path = agents_dir / f"{name}.md"
        steps.append({"what": f"subagent '{name}'", "path": str(path),
                      "action": "skip (exists)" if path.exists() else "create"})
    steps.append({"what": "UserPromptSubmit context guard hook",
                  "path": str(settings_path),
                  "action": "skip (installed)" if _has_our_hook(settings) else "add"})
    existing = settings.get("statusLine", {}).get("command", "")
    if MARKER in existing:
        sl = "skip (installed)"
    elif existing and not force_statusline:
        sl = "skip (you already have a status line; use --force-statusline to replace)"
    else:
        sl = "set"
    steps.append({"what": "status line (context meter)", "path": str(settings_path),
                  "action": sl})
    return steps


def apply(claude_dir=None, force_statusline=False, repo=None):
    if repo and not Path(repo).expanduser().is_dir():
        raise FileNotFoundError(f"no such folder: {repo}")
    steps = plan(claude_dir, force_statusline, repo)
    settings_path, agents_dir = targets(claude_dir, repo)
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name, body in AGENTS.items():
        path = agents_dir / f"{name}.md"
        if not path.exists():
            path.write_text(body, encoding="utf-8")

    settings = _load_settings(settings_path)
    if settings_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, settings_path.with_name(f"{settings_path.name}.{stamp}.bak"))
    if _has_our_hook(settings):
        # Upgrade an older install to the current, cross-platform command.
        for group in settings["hooks"]["UserPromptSubmit"]:
            for h in group.get("hooks", []):
                if MARKER in h.get("command", ""):
                    h["command"] = _python_cmd("hook")
    else:
        settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
            {"hooks": [{"type": "command", "command": _python_cmd("hook"), "timeout": 10}]})
    if steps[-1]["action"] == "set" or MARKER in settings.get("statusLine", {}).get("command", ""):
        settings["statusLine"] = {"type": "command", "command": _python_cmd("statusline")}
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return steps


def uninstall(claude_dir=None, repo=None):
    """Remove our hook and status line. Subagent files are left for you to delete."""
    settings_path, agents_dir = targets(claude_dir, repo)
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
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return [str(agents_dir / f"{n}.md") for n in AGENTS]
