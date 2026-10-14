"""Tests for monitor.py. Standard library only: run with `python3 -m unittest discover tests`."""
import builtins
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MONITOR = Path(__file__).resolve().parent.parent / "monitor.py"


def assistant(tokens, model="claude-sonnet-5", sidechain=False):
    entry = {"type": "assistant", "message": {"model": model, "usage": {"input_tokens": tokens}}}
    if sidechain:
        entry["isSidechain"] = True
    return entry


def context_output(text):
    """A /context result as Claude Code records it in the transcript."""
    return {"type": "system", "subtype": "local_command", "content": f"<local-command-stdout>{text}"}


def turn_context(model):
    return {"type": "turn_context", "payload": {"model": model}}


def token_count(tokens, window=None):
    info = {"last_token_usage": {"total_tokens": tokens}}
    if window:
        info["model_context_window"] = window
    return {"type": "event_msg", "payload": {"type": "token_count", "info": info}}


def claude_state():
    return {"tokens": None, "peak": 0, "model": None, "window": None}


def codex_state():
    return {"tokens": None, "model": None, "window": None}


class MonitorTest(unittest.TestCase):
    """Loads a fresh copy of monitor.py per test, with its home and config dir in a temporary folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.config_dir = self.home / "plugin-config"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def write(self, path, entries):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return path

    def append(self, path, entries):
        with open(path, "a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries))


class HelperTests(MonitorTest):
    def test_parse_count(self):
        self.assertEqual([self.m.parse_count(t) for t in ("107.9k", "200k", "1m", "1M", "950")],
                         [107_900, 200_000, 1_000_000, 1_000_000, 950])

    def test_percent_is_unknown_without_tokens_or_window(self):
        self.assertIsNone(self.m.percent(None, 200_000))
        self.assertIsNone(self.m.percent(10_000, None))
        self.assertEqual(self.m.percent(50_000, 200_000), 25)
        self.assertEqual(self.m.percent(300_000, 200_000), 100)

    def test_configured_window_uses_longest_matching_prefix(self):
        windows = {"claude-opus": 1, "claude-opus-5": 2}
        self.assertEqual(self.m.configured_window(windows, "claude-opus-5-5"), 2)
        self.assertEqual(self.m.configured_window(windows, "claude-opus-4"), 1)
        self.assertIsNone(self.m.configured_window(windows, "gpt-5"))
        self.assertIsNone(self.m.configured_window(windows, None))

    def test_load_context_windows(self):
        self.assertEqual(self.m.load_context_windows(), {})
        self.config_dir.mkdir()
        (self.config_dir / "config.json").write_text(json.dumps({"context_windows": {"claude-opus-5-5": 1_000_000, "bad": 0}}))
        self.assertEqual(self.m.load_context_windows(), {"claude-opus-5-5": 1_000_000})
        (self.config_dir / "config.json").write_text("not json")
        self.assertEqual(self.m.load_context_windows(), {})

    def test_claude_project_dir_replaces_every_non_alphanumeric(self):
        self.assertEqual(self.m.claude_project_dir("/Users/a.b/c_d e").name, "-Users-a-b-c-d-e")


class TranscriptReadingTests(MonitorTest):
    def test_only_appended_lines_are_read_and_a_partial_line_waits(self):
        path = self.write("t.jsonl", [assistant(50_000)])
        self.assertEqual(self.m.read_new_entries(path, self.m.parse_claude, claude_state)["tokens"], 50_000)
        with mock.patch("builtins.open") as opened:
            self.m.read_new_entries(path, self.m.parse_claude, claude_state)
        opened.assert_not_called()
        line = json.dumps(assistant(60_000))
        with open(path, "a") as f:
            f.write(line[:20])
        self.assertEqual(self.m.read_new_entries(path, self.m.parse_claude, claude_state)["tokens"], 50_000)
        with open(path, "a") as f:
            f.write(line[20:] + "\n")
        self.assertEqual(self.m.read_new_entries(path, self.m.parse_claude, claude_state)["tokens"], 60_000)

    def test_a_truncated_transcript_is_read_again_from_the_top(self):
        path = self.write("x.jsonl", [token_count(40_000), token_count(45_000)])
        self.m.read_new_entries(path, self.m.parse_codex, codex_state)
        path.write_text(json.dumps(token_count(5, window=100)) + "\n")
        state = self.m.read_new_entries(path, self.m.parse_codex, codex_state)
        self.assertEqual((state["tokens"], state["window"]), (5, 100))


class ClaudeTests(MonitorTest):
    def read(self, path):
        return self.m.read_new_entries(path, self.m.parse_claude, claude_state)

    def test_sidechain_requests_are_ignored(self):
        state = self.read(self.write("c.jsonl", [
            assistant(100_000, model="claude-opus-5-5"),
            context_output("100k/1m tokens (10%)"),
            assistant(9_000, model="claude-haiku-z", sidechain=True),
        ]))
        self.assertEqual((state["model"], state["window"], state["tokens"]), ("claude-opus-5-5", 1_000_000, 100_000))

    def test_context_output_sets_the_window_but_chat_text_does_not(self):
        state = self.read(self.write("c.jsonl", [
            assistant(10_000),
            {"type": "user", "message": {"content": "107.9k/1m tokens (11%)"}},
        ]))
        self.assertIsNone(state["window"])
        state = self.read(self.write("d.jsonl", [
            assistant(10_000),
            context_output("\x1b[38;2;1;1;1m30k/200k tokens (15%)\x1b[39m"),
        ]))
        self.assertEqual((state["tokens"], state["window"]), (30_000, 200_000))

    def test_a_model_switch_drops_the_window_and_peak(self):
        path = self.write("c.jsonl", [assistant(300_000, model="claude-opus-x"), context_output("300k/1m tokens (30%)")])
        self.read(path)
        self.append(path, [assistant(50_000, model="claude-sonnet-y")])
        state = self.read(path)
        self.assertEqual((state["window"], state["peak"]), (None, 50_000))

    def context(self, entries, windows=None):
        self.write(".claude/projects/-work/s1.jsonl", entries)
        return self.m.get_claude_context("s1", "/work", False, set(), windows or {})

    def test_window_from_context_output_comes_first(self):
        self.assertEqual(self.context([assistant(100_000, model="claude-opus-5-5"), context_output("100k/1m tokens (10%)")],
                                      {"claude-opus-5-5": 500_000}),
                         {"pct": 10, "used": 100_000, "window": 1_000_000})

    def test_configured_window_comes_next(self):
        self.assertEqual(self.context([assistant(100_000, model="claude-opus-5-5")], {"claude-opus-5-5": 1_000_000}),
                         {"pct": 10, "used": 100_000, "window": 1_000_000})

    def test_use_past_200k_implies_1m_else_200k(self):
        self.assertEqual(self.context([assistant(250_000)])["window"], 1_000_000)
        self.m._tails.clear()
        self.assertEqual(self.context([assistant(50_000)]), {"pct": 25, "used": 50_000, "window": 200_000})

    def test_no_usage_means_no_reading(self):
        self.assertIsNone(self.context([{"type": "user", "message": {"content": "hi"}}]))

    def test_transcript_found_by_session_id_in_another_project_folder(self):
        path = self.write(".claude/projects/-elsewhere/s2.jsonl", [assistant(1)])
        self.assertEqual(self.m.find_claude_transcript("s2", "/work", False, set()), path)

    def test_guessing_skips_claimed_sessions_and_needs_a_single_pane(self):
        older = self.write(".claude/projects/-work/a.jsonl", [assistant(1)])
        newer = self.write(".claude/projects/-work/b.jsonl", [assistant(1)])
        os.utime(older, (time.time() - 60, time.time() - 60))
        self.assertEqual(self.m.find_claude_transcript(None, "/work", True, set()), newer)
        self.assertEqual(self.m.find_claude_transcript(None, "/work", True, {"b"}), older)
        self.assertIsNone(self.m.find_claude_transcript(None, "/work", False, set()))


class CodexTests(MonitorTest):
    def read(self, path):
        return self.m.read_new_entries(path, self.m.parse_codex, codex_state)

    def test_window_comes_from_the_transcript(self):
        path = self.write(".codex/sessions/2026/10/13/rollout-abc.jsonl",
                          [turn_context("gpt-z"), {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                           token_count(95_000, window=475_000)])
        self.assertEqual(self.m.get_codex_context("abc", "/work", "", False, set(), {}),
                         {"pct": 20, "used": 95_000, "window": 475_000})
        self.assertEqual(self.m.find_codex_transcript("abc", "/work", "", False, set()), path)

    def test_no_stated_window_is_unknown_unless_configured(self):
        self.write(".codex/sessions/2026/10/13/rollout-abc.jsonl", [turn_context("gpt-z"), token_count(40_000)])
        self.assertIsNone(self.m.get_codex_context("abc", "/work", "", False, set(), {}))
        self.assertEqual(self.m.get_codex_context("abc", "/work", "", False, set(), {"gpt-z": 400_000})["pct"], 10)

    def test_a_model_switch_drops_the_stated_window(self):
        path = self.write("cx.jsonl", [turn_context("gpt-a"), token_count(40_000, 400_000), turn_context("gpt-a"), token_count(40_000)])
        self.assertEqual(self.read(path)["window"], 400_000)
        self.append(path, [turn_context("gpt-b"), token_count(40_000)])
        state = self.read(path)
        self.assertEqual((state["model"], state["window"]), ("gpt-b", None))

    def test_folder_guess_searches_every_rollout_and_caches_their_first_lines(self):
        sessions = self.home / ".codex/sessions/2026/10/13"

        def rollout(i, cwd, age):
            path = self.write(sessions / f"rollout-x-{i:04d}.jsonl", [{"type": "session_meta", "payload": {"id": f"id{i}", "cwd": cwd}}])
            os.utime(path, (time.time() - age, time.time() - age))
            return path

        target = rollout(0, "/work/old", 10_000)
        for i in range(1, 61):
            rollout(i, f"/work/other{i}", i)
        opened = []
        real_open = builtins.open
        with mock.patch("builtins.open", lambda f, *a, **k: (opened.append(f), real_open(f, *a, **k))[1]):
            self.assertEqual(self.m.find_codex_transcript(None, "/work/old", "", True, set()), target)
            self.assertEqual(len(opened), 61)
            opened.clear()
            self.assertEqual(self.m.find_codex_transcript(None, "/work/old", "", True, set()), target)
            self.assertIsNone(self.m.find_codex_transcript(None, "/work/old", "", True, {"id0"}))
            self.assertIsNone(self.m.find_codex_transcript(None, "/nowhere", "", True, set()))
            self.assertIsNone(self.m.find_codex_transcript(None, "/work/old", "", False, set()))
            self.assertEqual(opened, [])


class AntigravityTests(MonitorTest):
    def test_reading_against_a_1m_window(self):
        self.write(".gemini/antigravity-cli/brain/g1/.system_generated/logs/transcript.jsonl",
                   [{"input_tokens": 100_000, "cache_read_tokens": 50_000}])
        self.assertEqual(self.m.get_agy_context("g1"), {"pct": 15, "used": 150_000, "window": 1_000_000})
        self.assertIsNone(self.m.get_agy_context("missing"))
        self.assertIsNone(self.m.get_agy_context(None))


class ReportingTests(MonitorTest):
    def report_args(self, current):
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(self.m.report_metadata("w1:p1", current))
        return run.call_args[0][0]

    def test_low_use_reports_plain_tokens_and_clears_the_high_ones(self):
        args = self.report_args({"pct": 25, "used": 50_000, "window": 200_000})
        for expected in ("ctx_pct=25", "ctx_label=⛁ 25%", "ctx_display=25%", "ctx_used=50000", "ctx_window=200000"):
            self.assertIn(expected, args)
        self.assertEqual(args[args.index("ctx_label_high") - 1], "--clear-token")

    def test_use_above_50_reports_the_high_tokens(self):
        args = self.report_args({"pct": 51, "used": 102_000, "window": 200_000})
        self.assertIn("ctx_label_high=⛁ 51%", args)
        self.assertEqual(args[args.index("ctx_label") - 1], "--clear-token")

    def test_unknown_clears_every_token(self):
        args = self.report_args(None)
        self.assertEqual(args.count("--clear-token"), len(self.m.CTX_TOKENS))
        self.assertNotIn("--token", args)

    def test_a_rejected_report_returns_false(self):
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(self.m.report_metadata("w1:p1", None))
        with mock.patch.object(self.m.subprocess, "run", side_effect=OSError):
            self.assertFalse(self.m.report_metadata("w1:p1", None))


class UpdateAllPanesTests(MonitorTest):
    def setUp(self):
        super().setUp()
        self.transcript = self.write(".claude/projects/-work/s1.jsonl", [assistant(50_000)])
        self.panes = [{"pane_id": "w1:p1", "agent": "claude", "agent_session": {"value": "s1"}, "cwd": "/work"}]
        self.m.get_open_panes = lambda: self.panes
        self.sent = []
        self.accept = True
        self.m.report_metadata = lambda pane, current: self.sent.append((pane, current and current["pct"])) or self.accept

    def test_unchanged_readings_are_not_resent(self):
        self.m.update_all_panes()
        self.m.update_all_panes()
        self.assertEqual(self.sent, [("w1:p1", 25)])
        self.append(self.transcript, [assistant(100_000)])
        self.m.update_all_panes()
        self.assertEqual(self.sent, [("w1:p1", 25), ("w1:p1", 50)])

    def test_a_rejected_report_is_retried(self):
        self.accept = False
        self.m.update_all_panes()
        self.accept = True
        self.m.update_all_panes()
        self.m.update_all_panes()
        self.assertEqual(self.sent, [("w1:p1", 25), ("w1:p1", 25)])
        self.assertEqual(self.m._reported, {"w1:p1": (25, 50, 200_000)})

    def test_closed_panes_and_their_transcripts_are_forgotten(self):
        self.m.update_all_panes()
        self.panes.clear()
        self.m.update_all_panes()
        self.assertEqual((self.m._reported, self.m._tails), ({}, {}))

    def test_two_panes_in_one_folder_without_session_ids_get_no_guess(self):
        self.panes[:] = [{"pane_id": f"w1:p{i}", "agent": "claude", "cwd": "/work"} for i in (1, 2)]
        self.m.update_all_panes()
        self.assertEqual(self.sent, [("w1:p1", None), ("w1:p2", None)])

    def test_other_agents_are_skipped(self):
        self.panes[:] = [{"pane_id": "w1:p9", "agent": "shell", "cwd": "/work"}]
        self.m.update_all_panes()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
