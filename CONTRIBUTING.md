# Forking and contributing

CreditOptimiser is meant to be forked. If it doesn't work quite the way you want, make your
own version. Change the model routing to suit your work, add a preset for your team, or take
it somewhere new entirely. The MIT licence lets you use, change and share it, including
commercially.

This guide shows you around the code and the quickest ways to make it your own.

## Start a fork

1. Click **Fork** at the top of the [repository page](https://github.com/tradelynx/CreditOptimiser).
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR-NAME/CreditOptimiser.git
   cd CreditOptimiser
   python3 -m unittest discover -s tests     # everything should pass
   python3 -m creditopt serve --demo         # try it with sample data
   ```
3. **Turn on the automated tests.** In your fork, open the **Actions** tab and enable
   workflows (GitHub switches them off for new forks). Every push then runs the tests on
   macOS, Linux and Windows for free, so you can change things with confidence even without
   all three computers.
4. **Make it yours:**
   - Edit or delete `.github/FUNDING.yml`. It controls the **Sponsor** button and currently
     points to the original author's Buy Me a Coffee page.
   - Update the clone URLs in the README to point at your fork.

There's nothing to install: CreditOptimiser uses only Python's standard library.

## Code map

| File | What it does |
|---|---|
| `creditopt/models.py` | **Model catalogue and prices.** Names, model IDs, per-token prices, context windows and the "good for" descriptions |
| `creditopt/router.py` | **Picks a model for a task.** Keyword and scope signals with weights; easy to tune |
| `creditopt/runner.py` | **Runs tasks.** Presets, run options, sub-agent definitions, the plan, launching Claude Code, retries and the cost comparison |
| `creditopt/analysis.py` | **Usage stats and "where you can save" recommendations** from your transcripts |
| `creditopt/transcripts.py` | Reads Claude Code's transcripts (`~/.claude/projects/**/*.jsonl`) |
| `creditopt/hooks.py` | The Claude Code add-ons: the prompt-time context guard and the status line |
| `creditopt/installer.py` | Installs and uninstalls the add-ons, helper agents and auto-compact setting |
| `creditopt/server.py` | The local web server and its API (including the security checks) |
| `creditopt/web/index.html` | The whole dashboard: one HTML file with inline CSS and JavaScript, no build step |
| `creditopt/config.py` | Settings and their defaults |
| `creditopt/discover.py` | Finds git repositories for the dropdown |
| `creditopt/demo.py` | Generates the sample data for `--demo` |
| `creditopt/__main__.py` | The `python3 -m creditopt …` commands |
| `run_creditopt.py` | Launcher the Claude Code add-ons call, so they work from any folder |
| `tests/test_creditopt.py` | The tests |

## Easy things to customise

**Prices or a new model.** Edit `CATALOGUE` in `creditopt/models.py` and update
`PRICES_CHECKED`. Each model has a `tier` (1 = cheapest); routing and retries move up and down
the tiers.

**How tasks are routed.** `SIGNALS` in `creditopt/router.py` is a list of
`(pattern, weight, reason)`. Negative weights push towards cheaper models, positive towards
stronger ones. The thresholds are in `_pick()`. For example, add your own project's
vocabulary:
```python
(r"\b(migration|schema change)\b", 3, "database change"),
```
Then check a few of your real tasks: `python3 -m creditopt route "your task"`.

**Presets.** `PRESETS` in `creditopt/runner.py` defines Max savings, Balanced and Best
quality. Add your own (say, a "team" preset) and it appears wherever presets are offered.
The dashboard reads them from the server. Add a button for it in the `#preset` group in
`index.html`.

**Sub-agents.** `AGENT_DEFS` in `creditopt/runner.py` holds each sub-agent's description,
prompt, tools and model. Change their behaviour there. When each one is used is decided in
`plan_run()`.

**The helper agents installed into Claude Code** live in `AGENTS` in `creditopt/installer.py`.

**Recommendations.** Each "where you can save" item is one small function in
`creditopt/analysis.py` (`_context_bloat`, `_cache_rebuilds`, …). Add yours to
`recommendations()`.

**Settings.** Add a key with its default to `DEFAULTS` in `creditopt/config.py`. It's then
saved, loaded, and settable with `python3 -m creditopt config --set`.

**The dashboard.** Everything is in `creditopt/web/index.html`. Colours are CSS variables at
the top, with matching light and dark values. Reload the page to see changes; restart the
server after Python changes.

## Ground rules worth keeping

These keep the tool trustworthy for the people who use your fork:
- **Keep it local.** No telemetry, no uploading transcripts, and the server only listens on
  `127.0.0.1` behind its token (see [SECURITY.md](SECURITY.md)).
- **Stay honest about numbers.** Estimates are labelled as estimates.
- **Ask before acting.** Anything that edits a repository or Claude Code's settings shows its
  plan first and only touches what it created.
- **Stick to the standard library,** so installing stays a single `git clone`.
- **Add a test** for new behaviour, and keep all three operating systems green.

## Keep your fork up to date

To pull in improvements from the original:
```bash
git remote add upstream https://github.com/tradelynx/CreditOptimiser.git   # once
git fetch upstream
git merge upstream/main
```
Or click **Sync fork** on your fork's GitHub page.

## Sending changes back

Improvements that would help everyone are very welcome as pull requests: bug fixes, Windows
and Linux fixes, better routing signals, new recommendations. Keep each pull request to one
change, include a test, and describe what it fixes or adds. Not sure if something fits? Open
an issue first and ask.

## Licence

MIT. You can do almost anything with the code. The one requirement is to keep the copyright
and licence notice (the `LICENSE` file) in copies and substantial portions of it. A mention
that your fork is based on CreditOptimiser is appreciated, but not required.
