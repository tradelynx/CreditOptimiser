"""User settings shared by the dashboard, hook and status line."""

import json
import os
from pathlib import Path

DEFAULTS = {
    "context_budget": 150_000,   # compact before a session's context passes this
    "warn_ratio": 0.7,           # amber warning at this fraction of the budget
    "idle_minutes": 60,          # prompt cache is cold after this long
    "block_over_budget": False,  # if true, the hook refuses prompts past the budget
    "route_nudges": True,        # suggest a cheaper model when the prompt is trivial
    "repo_roots": [],            # folders to scan for git repos (empty = your home folder)
    "scan_depth": 4,             # how many folders deep to look for repos
    "auto_escalate": True,       # resume an unfinished run once on the next model up
    "claude_path": "",           # path to the claude command, if it isn't on PATH
    "run_defaults": {},          # your saved run options (preset + overrides)
}


def coerce(key, value):
    """Convert a user-supplied value to the type of the setting's default."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(default, dict):
        if not isinstance(value, dict):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    if isinstance(default, list):
        items = value if isinstance(value, list) else str(value).split(",")
        return [str(v).strip() for v in items if str(v).strip()]
    return type(default)(value)


def repo_roots(cfg):
    return cfg["repo_roots"] or [str(Path.home())]


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "creditopt"


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((config_dir() / "config.json").read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save(updates):
    cfg = load()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    return cfg
