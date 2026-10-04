"""Find git repositories on this machine, for the dashboard's repository picker."""

import os
import time
from pathlib import Path

# Folders that never hold your own repos, or are too big to walk.
SKIP = {"node_modules", "Library", "Applications", "Pictures", "Music", "Movies", "Photos",
        "venv", "env", "__pycache__", "vendor", "build", "dist", "target", "site-packages",
        "Pods", "DerivedData", "go", "snap", "OneDrive", "Dropbox", "iCloud Drive",
        "AppData", "Application Data", "Local Settings", "scoop"}


def find_git_repos(roots, max_depth=4, time_budget=4.0, limit=500):
    """Walk `roots` up to `max_depth` levels and return folders that contain .git.

    Stops descending once a repo is found, skips hidden and bulky folders, and
    gives up after `time_budget` seconds so the dashboard never hangs.
    """
    deadline = time.monotonic() + time_budget
    found = []
    stack = [(Path(r).expanduser(), 0) for r in roots]
    seen = set()
    while stack and len(found) < limit and time.monotonic() < deadline:
        folder, depth = stack.pop()
        try:
            key = folder.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            found.append(str(folder))
            continue
        if depth >= max_depth:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue
    return sorted(found, key=lambda p: Path(p).name.lower())


_cache = {"key": None, "repos": []}


def cached_repos(roots, max_depth, rescan=False):
    key = (tuple(roots), max_depth)
    if rescan or _cache["key"] != key:
        _cache["repos"] = find_git_repos(roots, max_depth)
        _cache["key"] = key
    return _cache["repos"]
