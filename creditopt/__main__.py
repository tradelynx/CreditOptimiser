"""creditopt command line."""

import argparse
import os
import time
import json
import sys

from . import __version__, config, hooks, installer, router, runner, server


def _money(x):
    return f"${x:,.2f}"


def cmd_report(args):
    data = server.report(args.claude_dir, args.days, args.repo)
    if args.json:
        print(json.dumps(data, indent=2))
        return
    t = data["totals"]
    if args.repo:
        print(f"Repository: {args.repo}")
    print(f"Last {args.days} days: {t['sessions']} sessions, {t['calls']} requests, "
          f"{_money(t['cost'])} API-equivalent, cache hit {t['cache_hit_rate']:.0%}")
    print(f"Busiest 5-hour window: {_money(t['peak_5h_window']['cost'])}")
    print("\nBy model:")
    for k, v in sorted(data["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        print(f"  {k:<8} {_money(v['cost']):>10}  {v['calls']} requests")
    print(f"\nRecommendations (est. saving {_money(t['potential_saving'])}):")
    for r in data["recommendations"] or [{"severity": "-", "title": "Nothing to flag.",
                                          "action": "", "saving": 0}]:
        print(f"  [{r['severity']}] {r['title']}  ({_money(r['saving'])})")
        if r["action"]:
            print(f"      → {r['action']}")


def cmd_route(args):
    rec = router.route(" ".join(args.task))
    if args.json:
        print(json.dumps(rec, indent=2))
        return
    print(f"{rec['name']}  ({', '.join(rec['reasons'])})")
    print(f"  {rec['command']}")
    for tip in rec["tips"]:
        print(f"  • {tip}")


def cmd_run(args):
    task = " ".join(args.task)
    repo = args.repo or os.getcwd()
    options = {"preset": args.preset, "model": args.model, "priority": args.priority,
               "self_test": args.self_test, "test_command": args.test_command,
               "review": args.review, "escalate": args.escalate, "access": args.access,
               "subagents": args.subagents}
    options = {k: v for k, v in options.items() if v is not None}
    plan = runner.plan_run(task, repo, options=options)
    print(f"Preset: {plan['options']['preset']}" + (f"  Tests: {plan['test_command']}" if plan["test_command"] else "  Tests: off")
          + f"  Review: {'on' if any(a['name'] == 'reviewer' for a in plan['agents']) else 'off'}"
          + f"  Retry up to: {plan['options']['escalate']}")
    print(f"Lead: {plan['model_name']}  Subagents: "
          f"{', '.join(a['name'] + ' (' + a['model'] + ')' for a in plan['agents']) or 'none'}")
    for why in plan["strategy"]:
        print(f"  • {why}")
    if args.dry_run:
        return
    try:
        run = runner.start(task, repo, options=options)
    except ValueError as e:
        sys.exit(str(e))
    seen = 0
    while True:
        snap = run.snapshot(seen)
        seen = snap["next"]
        for ev in snap["events"]:
            k = ev["kind"]
            if k == "text" and not ev.get("sub"):
                print(ev["text"])
            elif k == "tool":
                print(f"  {'  ' if ev.get('sub') else ''}[{ev['tool']}] {ev['text']}")
            elif k == "agent":
                print(f"  ⇢ subagent {ev['agent']}: {ev['text']}")
            elif k in ("escalate", "error"):
                print(f"  ! {ev['text']}")
        if snap["state"] != "running":
            print(f"\n{snap['state'].upper()} · ${snap['cost']:.2f} · "
                  + ", ".join(f"{k} ${v:.2f}" for k, v in snap["by_model"].items()))
            cmp = snap.get("comparison")
            if cmp:
                print("\nWhat the same work would have cost (estimate: this run's tokens repriced):")
                for row in cmp["rows"]:
                    mark = "▶" if row["actual"] else " "
                    print(f" {mark} {row['label']:<26} ${row['cost']:>7.2f}  {row['note']}")
                if cmp["saved_vs_opus"] > 0:
                    print(f"\nSaved ${cmp['saved_vs_opus']:.2f} ({cmp['saved_pct']:.0f}%) compared with running it all on Opus.")
            return
        time.sleep(0.5)


def cmd_install(args):
    if args.uninstall:
        left = installer.uninstall(args.claude_dir, args.repo)
        print("Removed the hook and status line. Subagent files were left in place:")
        print("\n".join(f"  {p}" for p in left))
        return
    if args.apply:
        try:
            steps = installer.apply(args.claude_dir, args.force_statusline, args.repo)
        except FileNotFoundError as e:
            sys.exit(str(e))
        where = f"for {args.repo} only" if args.repo else "for all of Claude Code"
        print(f"Installed {where}. Restart Claude Code to pick up the changes.")
    else:
        steps = installer.plan(args.claude_dir, args.force_statusline, args.repo)
        print("Dry run. Re-run with --apply to make these changes:")
    for s in steps:
        print(f"  {s['action']:<10} {s['what']}  ({s['path']})")


def cmd_config(args):
    updates = {}
    for pair in args.set or []:
        key, _, value = pair.partition("=")
        if key not in config.DEFAULTS:
            sys.exit(f"unknown setting {key}; choose from {', '.join(config.DEFAULTS)}")
        updates[key] = config.coerce(key, value)
    print(json.dumps(config.save(updates) if updates else config.load(), indent=2))


def main(argv=None):
    config.utf8_console()
    p = argparse.ArgumentParser(prog="creditopt", description="Get more from your Claude subscription.")
    p.add_argument("--version", action="version", version=f"CreditOptimiser {__version__}")
    p.add_argument("--claude-dir", help="Claude Code config dir (default ~/.claude)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="open the web dashboard")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--demo", action="store_true", help="use generated sample data")

    r = sub.add_parser("report", help="print a usage report")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--json", action="store_true")

    ro = sub.add_parser("route", help="recommend a model for a task")
    ro.add_argument("task", nargs="+")
    ro.add_argument("--json", action="store_true")

    rn = sub.add_parser("run", help="run a task with Claude Code on the most cost-effective setup")
    rn.add_argument("task", nargs="+")
    rn.add_argument("--preset", choices=list(runner.PRESETS), help="savings, balanced or quality (default: your saved defaults, else quality)")
    rn.add_argument("--model", choices=["haiku", "sonnet", "opus", "fable"], help="override the routed model")
    rn.add_argument("--priority", choices=["savings", "balanced", "quality"], help="how borderline model choices lean")
    rn.add_argument("--self-test", action=argparse.BooleanOptionalAction, default=None, help="let the run execute your tests")
    rn.add_argument("--test-command", help="test command to allow (default: detected)")
    rn.add_argument("--review", action=argparse.BooleanOptionalAction, default=None, help="have an Opus reviewer check the diff")
    rn.add_argument("--escalate", choices=["off", "sonnet", "opus", "fable"], help="highest model an unfinished run may retry on")
    rn.add_argument("--access", choices=["plan", "edit"], help="plan = read-only, edit = may edit files")
    rn.add_argument("--subagents", help="auto, off, or a list like scout,reviewer")
    rn.add_argument("--dry-run", action="store_true", help="show the plan without running")

    i = sub.add_parser("install", help="add subagents, hook and status line to Claude Code")
    i.add_argument("--apply", action="store_true", help="actually write the changes")
    i.add_argument("--force-statusline", action="store_true")
    i.add_argument("--uninstall", action="store_true")

    c = sub.add_parser("config", help="show or change settings")
    c.add_argument("--set", action="append", metavar="KEY=VALUE")

    repo_help = "only this repository (a folder you run Claude Code in)"
    for sp in (s, r, i, rn):
        sp.add_argument("--repo", metavar="PATH", help=repo_help)

    sub.add_parser("hook", help="(used by Claude Code) UserPromptSubmit hook")
    sub.add_parser("statusline", help="(used by Claude Code) status line")

    args = p.parse_args(argv)
    if getattr(args, "repo", None):
        args.repo = os.path.abspath(os.path.expanduser(args.repo))
    if args.cmd == "hook":
        return hooks.run_hook()
    if args.cmd == "statusline":
        return hooks.run_status_line()
    if args.cmd == "serve":
        claude_dir = args.claude_dir
        if args.demo:
            import tempfile
            from . import demo
            claude_dir = demo.generate(tempfile.mkdtemp(prefix="creditopt-demo-"))
        return server.serve(args.port, claude_dir, not args.no_browser, args.repo)
    handlers = {"run": cmd_run, "report": cmd_report, "route": cmd_route, "install": cmd_install, "config": cmd_config}
    if args.cmd not in handlers:
        p.print_help()
        return 0
    handlers[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
