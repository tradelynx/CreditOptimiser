# Security

CreditOptimiser runs entirely on your own computer. Here is what it can touch, how it's
protected, and what to watch out for.

## What it reads and writes
- **Reads** Claude Code's transcripts in `~/.claude/projects` to calculate usage. Nothing is
  uploaded anywhere: there's no telemetry, and it makes no network calls of its own.
- **Writes** its settings and run log to `~/.config/creditopt/`.
- **When you install the add-ons:** writes subagent files and a hook/status line to
  `~/.claude/` (or `<repo>/.claude/` for one repo). It backs up the settings file first.
- **When you run a task:** starts the `claude` command in the repository you picked. Claude
  Code's own permission system applies (see below).

It has no dependencies beyond Python's standard library, so there's no third-party
package supply chain to trust.

## The dashboard server
- **Local only.** It listens on `127.0.0.1`, so other machines on your network can't
  connect to it.
- **Requests must be addressed to localhost.** This blocks DNS-rebinding tricks.
- **A secret token is required.** Every API request must carry a random token created when
  the server starts. Only the dashboard page receives it. Requests from other origins are
  refused, including other local dev servers on different ports.
- **The page can't be embedded in other sites,** and its Content Security Policy limits what
  it can load.
- **The browser can't change which program is launched.** `claude_path` can only be set
  from the terminal.
- **Anyone who can run programs as your user can already do what this can.** The server
  doesn't defend against that, and isn't meant to.

## Running tasks: know what you're allowing
- **Runs use Claude Code's `acceptEdits` mode.** Claude can read and edit files in the chosen
  repo without asking. Review the changes with `git diff`, ideally on a branch.
- **Shell commands are refused unless you allow them.** With testing on, only your test
  command (and `git diff`/`git status` for the reviewer) is allowed, and only for that run.
- **Running tests runs the repo's code.** `npm test`, `pytest`, `make test` and so on execute
  whatever the repository defines. Only turn testing on for repositories you trust.
- **Untrusted repos can contain prompt injection.** Files written to steer an AI can try to
  get Claude to make unwanted edits. Use read-only "plan" mode, or don't run tasks in repos
  you don't trust.
- **Costs are real.** Runs use your Claude subscription or API credit. Every run shows what it
  cost, and the Max savings preset keeps spending down.

## Reporting a problem
Please report security issues privately, via GitHub's "Report a vulnerability" on this
repository's Security tab, rather than in a public issue.
