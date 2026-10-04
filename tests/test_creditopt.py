import json
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from creditopt import analysis, config, demo, discover, hooks, installer, models, router
from creditopt.transcripts import Session, filter_repo, in_repo, load_sessions, repositories


def write_transcript(folder, lines):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "s1.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    return path


def assistant(ts, ctx, model="claude-opus-5-5", msg_id="m1", req="r1", out=100):
    return {"type": "assistant", "sessionId": "s1", "timestamp": ts.isoformat(), "requestId": req,
            "message": {"id": msg_id, "model": model,
                        "usage": {"input_tokens": 0, "output_tokens": out, "cache_read_input_tokens": ctx,
                                  "cache_creation_input_tokens": 0}}}


class ModelsTest(unittest.TestCase):
    def test_family_mapping(self):
        self.assertEqual(models.family("claude-opus-4-8"), "opus")
        self.assertEqual(models.family("claude-sonnet-5-5[1m]"), "sonnet")
        self.assertEqual(models.family("claude-mythos-5-1"), "fable")
        self.assertEqual(models.family("gpt"), "other")

    def test_cost(self):
        self.assertAlmostEqual(models.cost("claude-opus-5-5", 1_000_000, 1_000_000), 24.0)
        self.assertAlmostEqual(models.cost("claude-haiku-4-5", cache_write_1h=1_000_000), 2.0)


class TranscriptTest(unittest.TestCase):
    def test_dedupes_streamed_entries(self):
        with tempfile.TemporaryDirectory() as d:
            ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
            user = {"type": "user", "sessionId": "s1", "timestamp": ts.isoformat(),
                    "message": {"role": "user", "content": "hello there"}}
            write_transcript(Path(d) / "projects" / "-x-app", [user, assistant(ts, 10), assistant(ts, 10)])
            s = load_sessions(d)["s1"]
            self.assertEqual(len(s.calls), 1)
            self.assertEqual(s.user_turns, 1)
            self.assertEqual(s.first_prompt, "hello there")
            self.assertEqual(s.project, "app")


class AnalysisTest(unittest.TestCase):
    def test_demo_report_has_recommendations(self):
        with tempfile.TemporaryDirectory() as d:
            demo.generate(d, days=20)
            report = analysis.build_report(load_sessions(d))
            self.assertGreater(report["totals"]["cost"], 0)
            kinds = {r["kind"] for r in report["recommendations"]}
            self.assertIn("context", kinds)
            self.assertLessEqual(report["totals"]["potential_saving"], report["totals"]["cost"])

    def test_compaction_saving_zero_under_budget(self):
        ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            write_transcript(Path(d) / "projects" / "p", [assistant(ts, 50_000, msg_id=f"m{i}", req=f"r{i}") for i in range(5)])
            s = load_sessions(d)["s1"]
            self.assertEqual(analysis.compaction_saving(s.calls, analysis.Settings()), 0)

    def test_compaction_saving_positive_when_long(self):
        ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
        lines = [assistant(ts + timedelta(seconds=i), 100_000 + i * 10_000, msg_id=f"m{i}", req=f"r{i}") for i in range(40)]
        with tempfile.TemporaryDirectory() as d:
            write_transcript(Path(d) / "projects" / "p", lines)
            s = load_sessions(d)["s1"]
            self.assertGreater(analysis.compaction_saving(s.calls, analysis.Settings()), 0)


class RepoTest(unittest.TestCase):
    def test_filter_by_repo(self):
        with tempfile.TemporaryDirectory() as d:
            demo.generate(d, days=10)
            sessions = load_sessions(d)
            only = filter_repo(sessions, "/home/dev/webshop")
            self.assertTrue(only)
            self.assertLess(len(only), len(sessions))
            self.assertTrue(all(s.cwd == "/home/dev/webshop" and s.project == "webshop" for s in only.values()))
            self.assertEqual(filter_repo(sessions, "/home/dev/web"), {})  # no prefix false-positives
            self.assertEqual(filter_repo(sessions, None), sessions)
            self.assertEqual({r["name"] for r in repositories(sessions)}, {"webshop", "api", "infra"})

    def test_subfolder_counts_as_repo(self):
        s = Session("s", "p", cwd="/code/app/src")
        self.assertTrue(in_repo(s, "/code/app"))
        self.assertTrue(in_repo(s, "/code/app/"))
        self.assertFalse(in_repo(s, "/code/ap"))


class DiscoverTest(unittest.TestCase):
    def test_finds_repos_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ["code/app/.git", "code/app/sub/.git", "work/api/.git",
                        "code/node_modules/dep/.git", ".hidden/x/.git", "a/b/c/d/e/deep/.git"]:
                (root / rel).mkdir(parents=True)
            found = discover.find_git_repos([d], max_depth=4)
            self.assertEqual(sorted(Path(p).name for p in found), ["api", "app"])

    def test_config_coerce(self):
        self.assertEqual(config.coerce("repo_roots", "~/Code, ~/Work,"), ["~/Code", "~/Work"])
        self.assertTrue(config.coerce("route_nudges", "yes"))
        self.assertEqual(config.coerce("scan_depth", "3"), 3)


class RouterTest(unittest.TestCase):
    def check(self, task, expected):
        self.assertEqual(router.route(task)["model"], expected, router.route(task))

    def test_routes(self):
        self.check("rename getUser to fetchUser in api.ts", "haiku")
        self.check("find where the retry limit is configured", "haiku")
        self.check("Add pagination to the orders endpoint and update its tests", "sonnet")
        self.check("Design the architecture for migrating auth across the entire monorepo", "opus")
        self.check("Investigate an intermittent race condition in the job queue", "opus")

    def test_command_is_shell_safe(self):
        self.assertIn('\\"', router.route('fix the "quoted" bug $(rm -rf /)')["command"])
        self.assertIn("\\$", router.route('fix $(rm -rf /)')["command"])


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_hook(self, ctx, prompt="continue", minutes_ago=1, **cfg):
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        path = write_transcript(Path(self.tmp.name) / "t", [assistant(now - timedelta(minutes=minutes_ago), ctx)])
        settings = {**config.DEFAULTS, **cfg}
        return hooks.evaluate_prompt({"transcript_path": str(path), "session_id": "s1", "prompt": prompt},
                                     settings, now)

    def test_quiet_when_small(self):
        self.assertIsNone(self.run_hook(10_000, route_nudges=False))

    def test_warns_once(self):
        self.assertIn("Consider /compact", self.run_hook(120_000)["systemMessage"])
        self.assertIsNone(self.run_hook(121_000))

    def test_over_budget_and_block(self):
        out = self.run_hook(200_000, block_over_budget=True)
        self.assertEqual(out["decision"], "block")
        self.assertIn("systemMessage", self.run_hook(200_000, block_over_budget=True))

    def test_idle_warning(self):
        out = self.run_hook(60_000, minutes_ago=120, route_nudges=False)
        self.assertIn("idle", out["systemMessage"])

    def test_route_nudge(self):
        out = self.run_hook(10_000, prompt="rename foo to bar in utils.py")
        self.assertIn("/model haiku", out["systemMessage"])

    def test_status_line(self):
        now = datetime.now(timezone.utc)
        path = write_transcript(Path(self.tmp.name) / "t", [assistant(now, 160_000)])
        line = hooks.status_line({"transcript_path": str(path), "model": {"display_name": "Opus"}}, config.DEFAULTS)
        self.assertIn("/compact", line)
        self.assertIn("160k/150k", line)


class InstallerTest(unittest.TestCase):
    def test_apply_is_idempotent_and_preserves_settings(self):
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / "settings.json"
            settings.write_text(json.dumps({"model": "sonnet", "statusLine": {"type": "command", "command": "mine"}}))
            installer.apply(d)
            installer.apply(d)
            data = json.loads(settings.read_text())
            self.assertEqual(data["model"], "sonnet")
            self.assertEqual(data["statusLine"]["command"], "mine")
            self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
            self.assertTrue((Path(d) / "agents" / "scout.md").exists())
            self.assertTrue(list(Path(d).glob("settings.json.*.bak")))
            installer.uninstall(d)
            data = json.loads(settings.read_text())
            self.assertNotIn("UserPromptSubmit", data.get("hooks", {}))
            self.assertEqual(data["statusLine"]["command"], "mine")

    def test_repo_install_is_local_to_repo(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            installer.apply(home, repo=repo)
            self.assertEqual(list(Path(home).iterdir()), [])
            local = json.loads((Path(repo) / ".claude" / "settings.local.json").read_text())
            self.assertIn("UserPromptSubmit", local["hooks"])
            self.assertTrue((Path(repo) / ".claude" / "agents" / "runner.md").exists())
            self.assertFalse((Path(repo) / ".claude" / "settings.json").exists())
            installer.uninstall(home, repo=repo)
            local = json.loads((Path(repo) / ".claude" / "settings.local.json").read_text())
            self.assertNotIn("UserPromptSubmit", local.get("hooks", {}))

    def test_repo_install_rejects_missing_folder(self):
        with self.assertRaises(FileNotFoundError):
            installer.apply(repo="/no/such/folder/anywhere")

    def test_plan_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            installer.plan(d)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
model = args[args.index("--model") + 1]
resumed = "--resume" in args
print(json.dumps({"type": "system", "subtype": "init", "model": model, "session_id": "sess-1"}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "scout", "description": "find files"}},
    {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}}))
status = "STATUS: DONE" if resumed else "STATUS: INCOMPLETE: ran out of ideas"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                  "total_cost_usd": 0.5, "session_id": "sess-1", "result": "did it\n" + status,
                  "modelUsage": {"claude-" + model + "-x": {"costUSD": 0.5}}}))
'''


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": self.tmp.name,
                                                 "CLAUDE_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()

    def test_plans(self):
        from creditopt import runner
        small = runner.plan_run("rename foo to bar in a.py", str(self.repo))
        self.assertEqual((small["model"], small["agents"]), ("haiku", []))
        big = runner.plan_run("Design the architecture for migrating auth across the entire monorepo", str(self.repo))
        self.assertEqual(big["model"], "opus")
        self.assertEqual([a["name"] for a in big["agents"]], ["scout", "implementer", "reviewer"])
        cheap = runner.plan_run("Design the architecture for migrating auth across the entire monorepo",
                                str(self.repo), options={"preset": "savings"})
        self.assertEqual([a["name"] for a in cheap["agents"]], ["scout", "implementer"])
        self.assertIsNone(cheap["escalate_to"])
        self.assertFalse(big["shell_allowed"])
        self.assertIn("Don't try to run tests", big["system_prompt"])
        self.assertEqual(runner.plan_run("rename foo", str(self.repo), "sonnet")["model"], "sonnet")

    def test_options(self):
        from creditopt import runner
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        task = "Add pagination to the orders endpoint"
        plan = runner.plan_run(task, str(self.repo))  # default preset: quality
        self.assertEqual(plan["test_command"], "npm test")
        self.assertIn("Bash(npm test)", runner.allowed_tools(plan))
        self.assertIn("Bash(git diff *)", runner.allowed_tools(plan))
        self.assertEqual([a["name"] for a in plan["agents"]], ["scout", "verifier", "reviewer"])
        off = runner.plan_run(task, str(self.repo), options={"self_test": False, "review": False, "subagents": "off"})
        self.assertEqual((off["agents"], off["test_command"], runner.allowed_tools(off)), ([], "", []))
        picked = runner.plan_run(task, str(self.repo), options={"subagents": "reviewer", "test_command": "make check"})
        self.assertEqual([a["name"] for a in picked["agents"]], ["reviewer"])
        self.assertEqual(picked["test_command"], "make check")
        ro = runner.plan_run(task, str(self.repo), options={"access": "plan"})
        self.assertEqual((ro["permission_mode"], ro["test_command"]), ("plan", ""))
        self.assertNotIn("reviewer", [a["name"] for a in ro["agents"]])
        fab = runner.plan_run(task, str(self.repo), options={"model": "opus", "escalate": "fable"})
        self.assertEqual(fab["escalate_to"], "fable")

    def test_saved_defaults(self):
        from creditopt import runner
        config.save({"run_defaults": {"preset": "savings", "review": True}})
        o = runner.resolve_options()
        self.assertEqual((o["preset"], o["review"], o["self_test"]), ("savings", True, False))
        self.assertEqual(runner.resolve_options({"preset": "quality"})["escalate"], "opus")
        self.assertEqual(runner.resolve_options({"escalate": "bogus"})["escalate"], "off")

    def test_detect_test_command(self):
        from creditopt import runner
        self.assertEqual(runner.detect_test_command(str(self.repo)), "")
        (self.repo / "go.mod").write_text("module x")
        self.assertEqual(runner.detect_test_command(str(self.repo)), "go test ./...")

    def test_shell_permission_adds_verifier(self):
        from creditopt import runner
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(npm test)"]}}))
        plan = runner.plan_run("Add pagination to the orders endpoint", str(self.repo))
        self.assertIn("verifier", [a["name"] for a in plan["agents"]])

    def test_parse_events(self):
        from creditopt import runner
        line = json.dumps({"type": "result", "is_error": False, "total_cost_usd": 1.0, "num_turns": 2,
                           "result": "x\nSTATUS: UNVERIFIED: run npm test", "modelUsage": {"claude-opus-5-5": {"costUSD": 1.0}}})
        ev = runner.parse_event(line)[0]
        self.assertEqual((ev["status"], ev["reason"], ev["by_model"]), ("UNVERIFIED", "run npm test", {"opus": 1.0}))
        self.assertEqual(runner.parse_event("not json"), [])

    def test_run_escalates_and_finishes(self):
        from creditopt import runner
        fake = Path(self.tmp.name) / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        config.save({"claude_path": str(fake)})
        run = runner.start("rename foo to bar in a.py", str(self.repo))
        for _ in range(100):
            if run.state != "running":
                break
            time.sleep(0.05)
        snap = run.snapshot()
        kinds = [e["kind"] for e in snap["events"]]
        self.assertEqual(snap["state"], "done")
        self.assertIn("escalate", kinds)
        self.assertIn("agent", kinds)
        self.assertEqual(snap["by_model"], {"haiku": 0.5, "sonnet": 0.5})
        self.assertEqual(runner.history()[0]["state"], "done")

    def test_start_validates(self):
        from creditopt import runner
        with self.assertRaises(ValueError):
            runner.start("", str(self.repo))
        with self.assertRaises(ValueError):
            runner.start("do it", "/no/such/repo")
