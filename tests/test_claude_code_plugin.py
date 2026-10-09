"""The Claude Code plugin: its Stop hook counts work in the transcript incrementally, reminds Claude once the
threshold is reached, starts counting again after Claude saves memory, never loops, and never fails a turn. The
manifests point at files that exist. Skipped where the plugin is not shipped (the server copy of the tests)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "integrations" / "claude-code"
SCRIPT = PLUGIN / "hooks" / "checkpoint.py"


def load():
    spec = importlib.util.spec_from_file_location("khala_checkpoint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tool(*names, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"content": [{"type": "tool_use", "name": n, "input": {}} for n in names]}}


def said(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


@unittest.skipUnless(SCRIPT.exists(), "the Claude Code plugin is not shipped here")
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="khala-plugin-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.transcript = self.dir / "session.jsonl"
        self.transcript.write_text("")
        self.env = {"CLAUDE_PLUGIN_DATA": str(self.dir / "data"), "CLAUDE_PLUGIN_OPTION_CHECKPOINT_EVERY": "10"}
        self.hook = load()

    def add(self, *entries, partial=""):
        with self.transcript.open("a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
            f.write(partial)

    def stop(self, **extra):
        event = dict({"session_id": "s-1", "transcript_path": str(self.transcript), "hook_event_name": "Stop",
                      "stop_hook_active": False}, **extra)
        return self.hook.run(event, self.env)

    def test_work_below_the_threshold_says_nothing_and_above_it_reminds_once(self):
        self.add(tool("Bash", "Read"), result(), tool("Edit"))
        self.assertIsNone(self.stop())
        self.add(said("now the tests"), tool("Bash", "Bash", "Bash", "Bash"))
        out = self.stop()                                   # 3 + 3 + 4 = 10
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertIn("memory_note", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.stop())                      # counting starts again

    def test_saving_memory_resets_the_count(self):
        self.add(tool(*["Bash"] * 9))
        self.add(tool("mcp__khala__memory_note"))
        self.add(tool("Bash"))
        self.assertIsNone(self.stop())
        self.add(tool(*["Bash"] * 9))
        self.assertIsNotNone(self.stop())
        self.add(tool(*["Bash"] * 12), tool("mcp__plugin_x_memory__memory_write"))
        self.assertIsNone(self.stop())

    def test_a_continued_turn_is_never_reminded_again(self):
        self.add(tool(*["Bash"] * 20))
        self.assertIsNone(self.stop(stop_hook_active=True))
        self.add(tool("Bash"))
        self.assertIsNotNone(self.stop())                   # the work was counted, not lost

    def test_only_complete_lines_count_and_subagents_do_not(self):
        self.add(tool(*["Bash"] * 9), tool(*["Bash"] * 5, sidechain=True),
                 partial=json.dumps(tool("Bash"))[:-5])
        self.assertIsNone(self.stop())
        with self.transcript.open("a") as f:                # the rest of the line arrives
            f.write(json.dumps(tool("Bash"))[-5:] + "\n")
        self.assertIsNotNone(self.stop())

    def test_zero_turns_it_off_and_bad_input_is_ignored(self):
        self.add(tool(*["Bash"] * 50))
        self.env["CLAUDE_PLUGIN_OPTION_CHECKPOINT_EVERY"] = "0"
        self.assertIsNone(self.stop())
        self.env["CLAUDE_PLUGIN_OPTION_CHECKPOINT_EVERY"] = "lots"
        self.add(tool(*["Bash"] * 15))
        self.assertIsNotNone(self.stop())                   # falls back to the default, 15
        self.assertIsNone(self.stop(hook_event_name="SessionEnd"))
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.hook.main(), 0)
        self.assertEqual(out.getvalue(), "")

    def test_a_rewritten_transcript_starts_over(self):
        self.add(tool(*["Bash"] * 8))
        self.assertIsNone(self.stop())
        self.transcript.write_text(json.dumps(tool("Bash")) + "\n")
        self.assertIsNone(self.stop())
        self.add(tool(*["Bash"] * 9))
        self.assertIsNotNone(self.stop())

    def test_it_runs_as_claude_code_runs_it(self):
        self.add(tool(*["Bash"] * 10))
        event = {"session_id": "s-2", "transcript_path": str(self.transcript), "hook_event_name": "Stop",
                 "stop_hook_active": False}
        env = dict({k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}, **self.env)
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event), capture_output=True,
                              text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("additionalContext", json.loads(done.stdout)["hookSpecificOutput"])


@unittest.skipUnless(SCRIPT.exists(), "the Claude Code plugin is not shipped here")
class ManifestTests(unittest.TestCase):
    def test_the_marketplace_and_plugin_point_at_real_files(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(p for p in market["plugins"] if p["name"] == "khala")
        self.assertEqual((ROOT / entry["source"]).resolve(), PLUGIN)
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "khala")
        option = manifest["userConfig"]["checkpoint_every"]
        self.assertEqual(option["default"], load().DEFAULT_EVERY)
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
        handler = hooks["Stop"][0]["hooks"][0]
        self.assertEqual(handler["args"], ["${CLAUDE_PLUGIN_ROOT}/hooks/checkpoint.py"])


if __name__ == "__main__":
    unittest.main()
