import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from creditopt import analysis, config, demo, hooks, installer, models, router
from creditopt.transcripts import load_sessions


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

    def test_plan_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            installer.plan(d)
            self.assertEqual(list(Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
