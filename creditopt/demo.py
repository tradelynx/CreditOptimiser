"""Generate realistic synthetic Claude Code transcripts, for demos and tests."""

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROMPTS = {
    "light": ["Where is the retry limit configured?", "Rename getUser to fetchUser in api.ts",
              "What does this regex do?", "Write a commit message for these changes",
              "Bump the version to 2.4.1"],
    "normal": ["Add pagination to the /orders endpoint and update its tests",
               "Fix the failing date parsing test in utils/dates.py",
               "Add a dark mode toggle to the settings page"],
    "heavy": ["Design the migration from REST to GraphQL across all services",
              "Investigate an intermittent deadlock in the job scheduler",
              "Review the auth module for security issues and plan fixes"],
}


def _entry(kind, sid, ts, **extra):
    return {"type": kind, "sessionId": sid, "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "uuid": str(uuid.uuid4()), "isSidechain": False, **extra}


def _assistant(sid, ts, model, ctx, new, out, sidechain=False, cold=False):
    write = ctx if cold else new
    read = 0 if cold else ctx - new
    msg = {"id": f"msg_{uuid.uuid4().hex[:20]}", "model": model, "role": "assistant",
           "content": [{"type": "text", "text": "…"}],
           "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": read,
                     "cache_creation_input_tokens": write,
                     "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write}}}
    e = _entry("assistant", sid, ts, message=msg, requestId=f"req_{uuid.uuid4().hex[:16]}")
    e["isSidechain"] = sidechain
    return e


def make_session(kind, model, start, rng, idle_break=False):
    sid = str(uuid.uuid4())
    prompt = rng.choice(PROMPTS[kind])
    turns = {"light": rng.randint(1, 4), "normal": rng.randint(5, 14), "heavy": rng.randint(18, 45)}[kind]
    out_scale = {"light": 300, "normal": 1500, "heavy": 2600}[kind]
    growth = {"light": 4_000, "normal": 9_000, "heavy": 11_000}[kind]
    ctx, ts, lines = 18_000, start, []
    for t in range(turns):
        ts += timedelta(minutes=rng.randint(1, 6))
        if idle_break and t == turns // 2:
            ts += timedelta(minutes=95)
        text = prompt if t == 0 else rng.choice(["continue", "looks good, now the tests", "that broke the build", "yes do it"])
        lines.append(_entry("user", sid, ts, message={"role": "user", "content": text}))
        cold = idle_break and t == turns // 2
        for step in range(rng.randint(1, 4)):
            ts += timedelta(seconds=rng.randint(5, 40))
            new = rng.randint(growth // 3, growth)
            ctx += new
            lines.append(_assistant(sid, ts, model, ctx, new, rng.randint(out_scale // 3, out_scale),
                                    cold=cold and step == 0))
        if kind != "light" and rng.random() < 0.25:
            sub_model = rng.choice([model, "claude-haiku-4-5"])
            for _ in range(rng.randint(2, 5)):
                ts += timedelta(seconds=8)
                lines.append(_assistant(sid, ts, sub_model, 12_000, 4_000, 400, sidechain=True))
    return sid, lines


def generate(target, days=30, seed=7):
    """Write ~days of usage into target/projects/... and return the target path."""
    rng = random.Random(seed)
    target = Path(target)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    projects = ["-home-dev-webshop", "-home-dev-billing-api", "-home-dev-infra"]
    for d in range(days, -1, -1):
        date = now - timedelta(days=d)
        if date.weekday() >= 5 and rng.random() < 0.6:
            continue
        for _ in range(rng.randint(1, 5)):
            kind = rng.choices(["light", "normal", "heavy"], [5, 4, 2])[0]
            model = rng.choices(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-fable-5-1"],
                                [6, 3, 0 if kind == "heavy" else 1.5, 0.4 if kind == "heavy" else 0])[0]
            start = date.replace(hour=rng.randint(8, 18)) if d else now - timedelta(hours=rng.randint(1, 6))
            sid, lines = make_session(kind, model, start, rng, idle_break=rng.random() < 0.15)
            project = rng.choice(projects)
            for line in lines:
                line["cwd"] = project.replace("-", "/")
            folder = target / "projects" / project
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return target
