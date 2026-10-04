# Changelog

All notable changes to CreditOptimiser are listed here. Versions follow
[semantic versioning](https://semver.org/): a new major number means you may need to change
how you use it.

## 1.0.0 (4 October 2026)

The first public release.

### Usage dashboard
- Reads Claude Code's transcripts and shows your usage by day, model, project and session,
  your busiest 5-hour window, and your prompt-cache hit rate.
- "Where you can save" lists ranked, specific fixes, each with an estimated saving: context
  bloat, big contexts re-loaded after a break, top-tier models used for light work, expensive
  sub-agents, and long sessions without a `/compact` or `/clear`.
- A repository dropdown lists the folders you've used Claude Code in and every git repository
  on your computer, and filters the dashboard to the one you pick.

### Run a task
- Describe a task and it picks the cheapest model likely to do it well (Haiku, Sonnet, Opus or
  Fable), then runs Claude Code in your chosen repository with live progress.
- Adds sub-agents where they save money: a Haiku scout for searching, Sonnet implementers
  when Opus leads, a Haiku verifier to run your tests, and an Opus reviewer to check the change.
- Presets (Max savings, Balanced, Best quality), with every setting adjustable and savable as
  your defaults.
- Every run ends with an honest status: done, done but not tested, or incomplete. Unfinished
  work is retried on a stronger model, up to a limit you choose.
- A cost comparison after each run shows what it cost against the same work on each model alone.
- You can switch tabs or reload the page during a run without losing it.

### Inside Claude Code (optional add-ons)
- A context guard that warns before prompts get expensive, when you resume a big session
  after a break, and when a trivial prompt is about to go to Opus.
- A context meter in the status line.
- Claude Code auto-compacts at your compact budget, not only when a conversation is nearly full.
- Cheap helper agents (scout, runner, architect).
- Install for all repositories, or for just one.

### Platforms and safety
- macOS, Linux and Windows, with automated tests on all three for every change.
- Runs entirely on your computer. The dashboard only answers its own page (secret token,
  same-origin and localhost checks).
- No dependencies beyond Python 3.9+.
