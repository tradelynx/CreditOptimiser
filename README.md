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

## Running tasks (the Run a task tab, or `creditopt run`)

Pick a repository, describe the task, and press **Run**. CreditOptimiser plans the cheapest
setup that's likely to do the job well, then runs Claude Code in that repo and streams its
progress, including the actual cost per model when it finishes.

**How much it does is up to you.** Pick a preset, or change any single setting:

| | Max savings | Balanced | Best quality (default) |
|---|---|---|---|
| Borderline model choices lean to | cheaper model | neither | stronger model |
| Sub-agents | where they help | where they help | where they help |
| Runs your tests (only that command, only for that run) | no | yes | yes |
| Opus reviewer checks the finished change | no | no | yes (skipped for small mechanical tasks) |
| Unfinished work is retried on a stronger model, up to | no retry | Opus | Opus |

Other settings: a fixed model, sub-agents off or hand-picked, a custom test command, a
read-only "plan" mode, and allowing retries up to Fable. **Save as my defaults** remembers
your choice. From the terminal, use `--preset`, `--model`, `--priority`, `--[no-]self-test`,
`--test-command`, `--[no-]review`, `--escalate`, `--access` and `--subagents`.

**Our quality commitment.** CreditOptimiser saves credit by cutting waste, never corners:
- It picks the cheapest model that will do the task *well*. On Best quality, borderline
  calls go to the stronger model.
- The savings come from cheap helpers doing the searching, cheaper models doing routine
  edits, and clearing stale context. The model doing the thinking keeps its full ability,
  and every run is told to do the task properly, not to economise on tokens.
- When you allow it, the work is checked: your tests are run, and an Opus reviewer reads
  the diff against your request.
- Unfinished work is resumed on a stronger model, keeping what's been done.
- It reports honestly: done, done but not tested, or incomplete, plus what wasn't verified
  and the exact cost.

What each run does:
- **Lead model:** chosen by the router (you can override it). Haiku handles small
  mechanical tasks on its own, with no sub-agents.
- **Sub-agents**, added only where they pay off:
  - `scout` (Haiku) does the searching, so file dumps don't sit in the expensive context.
  - `implementer` (Sonnet) makes clearly specified edits when Opus leads (half Opus's
    price), and runs in parallel for tasks with several independent parts.
  - `verifier` (Haiku) runs your tests when testing is on.
  - `reviewer` (Opus) reviews the finished diff against your request when review is on.
- **Permissions:** runs can read and edit files (Claude Code's `acceptEdits` mode). Other
  shell commands are refused unless the repo's settings allow them. Turning testing on allows
  only your test command (plus `git diff`/`git status` for the reviewer), and only for that run. Review the changes with
  `git diff`. Working on a branch is a good habit.
- **Quality guard:** every run ends with a status:
  - *done*: finished and checked.
  - *done, not tested*: finished, but the checks couldn't run. It tells you which to run.
  - *incomplete*: not finished. An incomplete or failed run is resumed once on the next
    model up, keeping the work so far. It never escalates automatically to Fable; choose
    Fable yourself if you want it. Turn retries off with `auto_escalate=false`.

```bash
python3 -m creditopt run --repo ~/Code/shop "Add a CSV export button to the reports page"
python3 -m creditopt run --repo ~/Code/shop --dry-run "…"   # show the plan only
python3 -m creditopt run --model opus "…"                    # override the model
```

The model choice is a heuristic, so it will sometimes be wrong. The retry catches runs where
the model was too small, and the override is there when you know better. Runs are logged to
`~/.config/creditopt/runs.jsonl`.

## Choosing a repository

The dashboard has a **repository dropdown** at the top. It lists the folders you've used
Claude Code in (with their usage), then every other git repository it finds on your computer.
By default it scans your home folder 4 levels deep; change that under **Setup → Settings**,
or click **Rescan** after cloning something new.

Picking a repository:
- filters the dashboard to that repo's sessions only
- points the **Setup** tab's Install/Remove buttons at that repo. Agents go in
  `<repo>/.claude/agents/`, and the hook and status line go in
  `<repo>/.claude/settings.local.json` (your personal settings file there, not committed),
  so they only apply when Claude Code runs in that repo.

The same works from the terminal (run these from the creditoptimiser folder):

```bash
python3 -m creditopt serve --repo ~/Code/shop          # open the dashboard on one repo
python3 -m creditopt report --repo ~/Code/shop
python3 -m creditopt install --repo ~/Code/shop --apply
python3 -m creditopt config --set repo_roots="~/Code, ~/Work"
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
- **Run a task:** describe a task, preview the plan (lead model, sub-agents, and the reasons),
  then run it in the selected repository with live progress.
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

## Security

Everything runs on your machine, and the dashboard can only be used by its own page. Runs
can edit files in the repo you pick, and running tests executes that repo's code. Read
[SECURITY.md](SECURITY.md) before running tasks in repositories you don't fully trust.

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
| `repo_roots` | home folder | folders to scan for git repositories |
| `scan_depth` | 4 | how many folders deep to scan |
| `auto_escalate` | true | resume an unfinished run once on the next model up (never Fable) |
| `claude_path` | auto | path to the `claude` command if it isn't found automatically |

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
