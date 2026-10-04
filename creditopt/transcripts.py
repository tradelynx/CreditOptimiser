"""Parse Claude Code transcripts (~/.claude/projects/**/*.jsonl) into API calls."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def default_claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


@dataclass
class Call:
    """One billed API request (deduplicated across streamed transcript entries)."""
    session_id: str
    project: str
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    sidechain: bool  # True when made by a subagent

    @property
    def context_tokens(self):
        """Tokens the model had to read for this request: the context size."""
        return self.input_tokens + self.cache_read + self.cache_write_5m + self.cache_write_1h


@dataclass
class Session:
    session_id: str
    project: str
    calls: list = field(default_factory=list)
    first_prompt: str = ""
    user_turns: int = 0
    compactions: int = 0
    cwd: str = ""        # directory Claude Code was started in


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _prompt_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def _is_real_prompt(entry):
    """A user turn typed by the human, not a tool result or injected meta message."""
    msg = entry.get("message") or {}
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str) and not content.startswith("<")


def _call_from(entry, project):
    msg = entry.get("message") or {}
    usage = msg.get("usage")
    ts = _parse_ts(entry.get("timestamp"))
    if not usage or ts is None or msg.get("model") in (None, "<synthetic>"):
        return None
    write_total = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    w1h = split.get("ephemeral_1h_input_tokens") or 0
    w5m = split.get("ephemeral_5m_input_tokens")
    if w5m is None:
        w5m = max(write_total - w1h, 0)
    return Call(
        session_id=entry.get("sessionId", ""),
        project=project,
        timestamp=ts,
        model=msg.get("model", ""),
        input_tokens=usage.get("input_tokens") or 0,
        output_tokens=usage.get("output_tokens") or 0,
        cache_read=usage.get("cache_read_input_tokens") or 0,
        cache_write_5m=w5m,
        cache_write_1h=w1h,
        sidechain=bool(entry.get("isSidechain")),
    )


def project_name(path, projects_dir):
    """'-home-me-code-app' directory -> 'app' style short name."""
    try:
        top = path.relative_to(projects_dir).parts[0]
    except ValueError:
        top = path.parent.name
    parts = [p for p in top.split("-") if p]
    return parts[-1] if parts else top


def load_sessions(claude_dir=None, since=None):
    """Read every transcript and return {session_id: Session}."""
    projects_dir = Path(claude_dir or default_claude_dir()) / "projects"
    sessions = {}
    seen = set()
    if not projects_dir.is_dir():
        return sessions
    for path in sorted(projects_dir.rglob("*.jsonl")):
        if since and path.stat().st_mtime < since.timestamp():
            continue
        project = project_name(path, projects_dir)
        for entry in read_jsonl(path):
            sid = entry.get("sessionId")
            if not sid:
                continue
            session = sessions.get(sid)
            if session is None:
                session = sessions[sid] = Session(sid, project)
            if not session.cwd and entry.get("cwd"):
                session.cwd = entry["cwd"]
            kind = entry.get("type")
            if kind == "assistant":
                msg = entry.get("message") or {}
                key = (msg.get("id"), entry.get("requestId"))
                if key in seen:
                    continue  # one response is logged once per content block
                seen.add(key)
                call = _call_from(entry, project)
                if call and (since is None or call.timestamp >= since):
                    session.calls.append(call)
            elif kind == "user" and not entry.get("isSidechain"):
                if entry.get("isCompactSummary"):
                    session.compactions += 1
                elif _is_real_prompt(entry):
                    session.user_turns += 1
                    if not session.first_prompt:
                        session.first_prompt = _prompt_text(entry["message"])[:200]
            elif kind == "system" and entry.get("subtype") == "compact_boundary":
                session.compactions += 1
    for s in sessions.values():
        s.calls.sort(key=lambda c: c.timestamp)
        if s.cwd:
            # The real folder name beats the mangled transcript directory name.
            s.project = Path(s.cwd).name or s.project
            for c in s.calls:
                c.project = s.project
    return {k: v for k, v in sessions.items() if v.calls}


def _path_forms(path):
    """Spellings of a path to compare by (case-insensitive on Windows)."""
    p = Path(path).expanduser()
    forms = {os.path.normcase(os.path.normpath(os.path.abspath(p)))}
    try:
        forms.add(os.path.normcase(str(p.resolve())))
    except OSError:
        pass
    return forms


def in_repo(session, repo):
    """True if the session was started in `repo` or one of its subfolders."""
    if not session.cwd:
        return False
    cwd_forms = _path_forms(session.cwd)
    for root in _path_forms(repo):
        for cwd in cwd_forms:
            if cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep):
                return True
    return False


def filter_repo(sessions, repo):
    """Keep only the sessions that ran inside `repo` (no filter when repo is falsy)."""
    if not repo:
        return sessions
    return {k: s for k, s in sessions.items() if in_repo(s, repo)}


def repositories(sessions):
    """Folders Claude Code has been used in, most-used first."""
    from .analysis import call_cost
    totals = {}
    for s in sessions.values():
        if s.cwd:
            totals[s.cwd] = totals.get(s.cwd, 0.0) + sum(call_cost(c) for c in s.calls)
    return [{"path": p, "name": Path(p).name or p, "cost": round(v, 2)}
            for p, v in sorted(totals.items(), key=lambda kv: -kv[1])]


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def last_context_tokens(transcript_path):
    """Context size of the latest main-thread request in one transcript, or 0."""
    latest = 0
    model = ""
    for entry in read_jsonl(transcript_path):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        call = _call_from(entry, "")
        if call:
            latest, model = call.context_tokens, call.model
    return latest, model
