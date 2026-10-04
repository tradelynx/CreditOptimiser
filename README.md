# CreditOptimiser

Get more out of your Claude subscription. CreditOptimiser reads Claude Code's local
transcripts and shows where your usage goes. It recommends the cheapest model that can
handle a task, and it adds hooks to Claude Code that tell you when to `/compact` or `/clear`.

Pure Python standard library, no dependencies. Everything runs on `127.0.0.1`, and your
transcripts never leave your machine.

## Quick start

```bash
git clone <this repo> && cd CreditOptimiser
pip install -e .                 # or skip this and use `python3 -m creditopt` instead of `creditopt`

creditopt serve                  # opens the dashboard at http://127.0.0.1:8765
creditopt serve --demo           # try it with generated sample data
creditopt install                # dry run: shows what it would add to Claude Code
creditopt install --apply        # add the hook, status line and subagents
```

## What it does

### 1. Dashboard (`creditopt serve`)
- **Overview:** API-equivalent usage, your busiest 5-hour window (subscription limits
  reset on a 5-hour cycle), cache hit rate, daily usage by model, and top projects.
- **Where you can save:** ranked recommendations, each with an estimated saving and a
  concrete fix:
  - *Context bloat:* sessions that ran past your compact budget. The saving is estimated by
    replaying each session as if it had compacted at the budget.
  - *Cold-cache resumes:* big contexts re-cached after an idle break.
  - *Over-powered sessions:* light sessions that ran on Opus or Fable.
  - *Expensive subagents:* exploration subagents running on Sonnet or above.
  - *Long sessions:* 30+ turns with no `/compact` or `/clear`.
- **Sessions:** every session, with a peak-context meter against your budget.
- **Task router:** describe a task and get a model recommendation, the reasons for it, and
  a ready-to-paste `claude --model …` command.
- **Setup:** see the install plan and edit your settings.

### 2. Model routing (`creditopt route "…"`)
An offline heuristic, so it's free and explains itself. It scores a task on its kind of
work (mechanical vs. building vs. design/debugging), scope (files, "entire codebase"), and
ambiguity:

| Model | Picked for |
|---|---|
| Haiku 4.5 | lookups, renames, formatting, commit messages, routine commands |
| Sonnet 5.5 | everyday features, bug fixes, tests, refactors |
| Opus 5.5 | architecture, planning, hard debugging, security, migrations, reviews |
| Fable 5.1 | only as an escalation when Opus has already struggled |

### 3. Automation inside Claude Code (`creditopt install --apply`)
- **Context guard** (`UserPromptSubmit` hook). Before each prompt, it checks the session's
  context. It warns once at 70% of your budget and on every prompt past it, with an option
  to hold the first prompt past it so you can `/compact` first. It also warns when you
  resume a big session whose cache has gone cold, and suggests `/model haiku` when a
  trivial prompt is about to go to Opus.
- **Status line:** `Opus 5.5 · ctx ███████░░░ 104k/150k → compact soon · ≈$2.41 API-equiv`
- **Subagents:** `scout` (Haiku, read-only exploration) and `runner` (Haiku, runs
  tests/builds and reports only the failures) keep noisy output out of your main context
  cheaply. `architect` (Opus) gives Sonnet sessions deep reasoning for a single step.

The installer never overwrites an existing status line (unless you pass `--force-statusline`)
or existing agent files, and it backs up `settings.json` before writing.
`creditopt install --uninstall` removes the hook and status line.

> Claude Code doesn't let hooks run `/compact` or switch models themselves, so the tool
> tells you when to and makes it a single keystroke. Claude Code's own auto-compaction
> still runs near the context limit. CreditOptimiser nudges you to compact far earlier,
> which is where the savings are.

## Settings

`creditopt config --set context_budget=120000`, or use the dashboard's Setup tab. Stored in
`~/.config/creditopt/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `context_budget` | 150000 | compact before a session's context passes this |
| `warn_ratio` | 0.7 | early warning at this fraction of the budget |
| `idle_minutes` | 60 | warn about a cold cache after this much idle time |
| `block_over_budget` | false | hold the first prompt sent past the budget |
| `route_nudges` | true | suggest a cheaper model for trivial prompts |

## How the numbers work
- Usage comes from the `usage` block of each API response in `~/.claude/projects/**/*.jsonl`,
  deduplicated per request. Subagent transcripts are included.
- Cost is **API-equivalent**: first-party API prices (see `creditopt/models.py`), including
  cache reads and cache writes. Pro/Max plans don't bill per token, but your usage limits
  are consumed in proportion to the same compute. That makes this the right yardstick for
  comparing models and habits, even though it isn't a bill.
- Savings are estimates and can overlap (for example, a bloated session that also went cold).

## Development
```bash
python3 -m unittest discover -s tests
```
