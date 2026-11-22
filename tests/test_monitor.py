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

MONITOR = Path(__file__).resolve().parent.parent / "herdr-app/mods/context-monitor/monitor.py"


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
        self.config_dir = self.home / "mod-config"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
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
        self.assertEqual(self.m.load_context_windows(self.config_dir), {})
        self.config_dir.mkdir()
        (self.config_dir / "config.json").write_text(json.dumps({"context_windows": {"claude-opus-5-5": 1_000_000, "bad": 0}}))
        self.assertEqual(self.m.load_context_windows(self.config_dir), {"claude-opus-5-5": 1_000_000})
        (self.config_dir / "config.json").write_text("not json")
        self.assertEqual(self.m.load_context_windows(self.config_dir), {})

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
        now = time.time()
        os.utime(older, (now - 60, now - 60))
        self.assertEqual(self.m.find_claude_transcript(None, "/work", True, set()), newer)
        self.assertEqual(self.m.find_claude_transcript(None, "/work", True, {"b"}), older)
        self.assertIsNone(self.m.find_claude_transcript(None, "/work", False, set()))
        self.assertIsNone(self.m.find_claude_transcript(None, "/work", True, set(), started_at=now + 10))
        self.assertEqual(self.m.find_claude_transcript(None, "/work", True, set(), started_at=now - 10), newer)


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

    def test_title_match_cannot_select_a_session_in_another_folder(self):
        self.write(".codex/session_index.jsonl", [{"id": "old", "thread_name": "DeuceMate"}])
        self.write(".codex/sessions/rollout-old.jsonl", [
            {"type": "session_meta", "payload": {"id": "old", "cwd": "/Code/DeuceMate"}},
            token_count(53_000, 100_000),
        ])
        new = self.write(".codex/sessions/rollout-new.jsonl", [
            {"type": "session_meta", "payload": {"id": "new", "cwd": "/Code/DeuceMate3/DeuceMate"}},
            token_count(10_000, 100_000),
        ])
        self.assertEqual(self.m.find_codex_transcript(None, "/Code/DeuceMate3/DeuceMate", "DeuceMate3", True, set()), new)
        self.assertEqual(self.m.get_codex_context(None, "/Code/DeuceMate3/DeuceMate", "DeuceMate3", True, set(), {})["pct"], 10)
        self.assertIsNone(self.m.find_codex_transcript(None, "/Code/DeuceMate4", "DeuceMate3", True, set()))
        self.assertIsNone(self.m.find_codex_transcript(None, "/Code/DeuceMate3/DeuceMate", "DeuceMate3", True, set(),
                                                       started_at=time.time() + 10))

    def test_ambiguous_title_matches_in_same_folder_are_not_guessed(self):
        self.write(".codex/session_index.jsonl", [
            {"id": "one", "thread_name": "DeuceMate"},
            {"id": "two", "thread_name": "DeuceMate"},
        ])
        for session_id in ("one", "two"):
            self.write(f".codex/sessions/rollout-{session_id}.jsonl", [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": "/work"}},
            ])
        self.assertIsNone(self.m.find_codex_transcript(None, "/work", "DeuceMate", True, set()))
        self.assertEqual(self.m.find_codex_transcript(None, "/work", "DeuceMate", True, {"one"}).name,
                         "rollout-two.jsonl")

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


def agy_turn(tokens, cache=0, model="gemini-4-neon-xhigh"):
    return {"type": "PLANNER_RESPONSE", "source": "MODEL", "model": model,
            "input_tokens": tokens, "cache_read_tokens": cache}


UUID_A = "b10f6ac2-eedb-41af-b3f6-3e01d11a45ec"
UUID_B = "cec7a6b0-3bb8-4aab-809a-c405449c9350"


class AntigravityTests(MonitorTest):
    def transcript(self, uuid, entries):
        return self.write(f".gemini/antigravity-cli/brain/{uuid}/.system_generated/logs/transcript.jsonl", entries)

    def last_conversations(self, mapping):
        self.write(".gemini/antigravity-cli/cache/last_conversations.json", [])
        (self.home / ".gemini/antigravity-cli/cache/last_conversations.json").write_text(json.dumps(mapping))

    def context(self, session=UUID_A, cwd="/work", may_guess=True, claimed=(), windows=None):
        return self.m.get_antigravity_context(session, cwd, may_guess, set(claimed), windows or {})

    def test_use_is_input_plus_cache_of_the_latest_model_turn(self):
        self.transcript(UUID_A, [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "hi"},
            agy_turn(16_000, 0), agy_turn(28_000, 132_000),
            {"type": "GENERIC", "source": "MODEL", "content": "output"},
        ])
        self.assertEqual(self.context(), {"pct": 16, "used": 160_000, "window": 1_000_000})

    def test_window_by_model(self):
        self.transcript(UUID_A, [agy_turn(50_000, model="claude-opus-5-5-high")])
        self.assertEqual(self.context()["window"], 200_000)
        self.assertEqual(self.context(windows={"claude-opus-5-5": 1_000_000})["window"], 1_000_000)
        self.transcript(UUID_B, [agy_turn(50_000, model="gemini-pro-orchestrator-g1-pro")])
        self.assertEqual(self.context(UUID_B)["window"], 1_000_000)
        self.assertEqual(self.context(UUID_B, windows={"gemini": 2_000_000})["window"], 2_000_000)

    def test_a_peak_past_200k_implies_1m_even_after_a_model_switch(self):
        path = self.transcript(UUID_A, [agy_turn(250_000, model="claude-opus-5-5-high")])
        self.context()
        self.append(path, [agy_turn(10_000, 240_000, model="claude-sonnet-5")])
        self.assertEqual(self.context(), {"pct": 25, "used": 250_000, "window": 1_000_000})

    def test_no_model_turn_means_no_reading(self):
        self.transcript(UUID_A, [{"type": "USER_INPUT", "content": "hi"}])
        self.assertIsNone(self.context())

    def test_session_is_the_one_conversation_the_process_has_open(self):
        def lsof(*ids):
            lines = [f"n/h/.gemini/antigravity-cli/conversations/{i}.db{s}" for i in ids for s in ("", "-wal", "-shm")]
            lines += [f"n/h/.gemini/antigravity-cli/brain/{i}" for i in ids]
            return mock.Mock(stdout="p1\n" + "\n".join(lines) + "\n")
        with mock.patch.object(self.m.subprocess, "run", return_value=lsof(UUID_A)):
            self.assertEqual(self.m.antigravity_session_id(1, "/work"), UUID_A)
        # several conversations open: the one last run in the folder, if there is exactly one
        with mock.patch.object(self.m.subprocess, "run", return_value=lsof(UUID_A, UUID_B)):
            self.assertIsNone(self.m.antigravity_session_id(1, "/work"))
            self.last_conversations({"/work": UUID_B})
            self.assertEqual(self.m.antigravity_session_id(1, "/work"), UUID_B)
            self.assertIsNone(self.m.antigravity_session_id(1, "/elsewhere"))
        with mock.patch.object(self.m.subprocess, "run", side_effect=OSError):
            self.assertIsNone(self.m.antigravity_session_id(1, "/work"))

    def test_guessing_uses_the_folders_last_conversation_for_a_single_pane(self):
        path = self.transcript(UUID_A, [agy_turn(100_000)])
        self.last_conversations({"/work": UUID_A})
        now = time.time()
        os.utime(path, (now - 30, now - 30))
        self.assertEqual(self.context(session=None)["pct"], 10)
        self.assertIsNone(self.context(session=None, may_guess=False))
        self.assertIsNone(self.context(session=None, claimed=[UUID_A]))
        self.assertIsNone(self.context(session=None, cwd="/elsewhere"))
        self.assertIsNone(self.m.get_antigravity_context(None, "/work", True, set(), {}, started_at=now))
        self.assertEqual(self.m.get_antigravity_context(None, "/work", True, set(), {}, started_at=now - 60)["pct"], 10)

    def test_a_conversation_without_a_transcript_has_no_reading(self):
        self.assertIsNone(self.context())


class DesktopMonitorTests(MonitorTest):
    """One-shot readings for the desktop mod, by herdr-core terminal address."""

    def setUp(self):
        super().setUp()
        self.transcript = self.write(".claude/projects/-work/s1.jsonl", [assistant(50_000)])
        self.write(".claude/sessions/101.json", [{"pid": 101, "sessionId": "s1"}])
        self.terminals = [{"address": "t1", "agent": "claude", "pid": 101, "cwd": "/work", "title": ""}]
        self.m.get_app_terminals = lambda: [dict(t) for t in self.terminals]
        self.state = self.home / "state.json"

    def poll(self, module=None, args=()):
        module = module or self.m
        with mock.patch("builtins.print") as printed:
            module.main(["--state", str(self.state), *args])
        return json.loads(printed.call_args[0][0])["readings"]

    def fresh_module(self):
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.get_app_terminals = self.m.get_app_terminals
        return module

    def test_terminals_come_from_herdr_core_with_the_agents_own_folder(self):
        listing = {"terminals": [
            {"address": "t1", "agent": "claude-code", "state": "live", "cwd": "/home"},
            {"address": "t2", "agent": "zsh", "state": "live"},
            {"address": "t3", "agent": "codex", "state": "finished"},
        ]}
        detail = {"cwd": "/home", "title": "fix login", "foreground": {"pid": 101, "cwd": "/work"}}
        calls = []
        # setUp replaced get_app_terminals on self.m; a fresh copy has the real one
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.run_json = lambda args: calls.append(args[1:]) or (listing if args[1] == "ls" else detail)
        self.assertEqual(module.get_app_terminals(),
                         [{"address": "t1", "agent": "claude", "pid": 101, "cwd": "/work", "title": "fix login"}])
        self.assertEqual(calls, [["ls", "--json"], ["get", "t1", "--json"]])

    def test_antigravity_terminals_are_listed_with_the_agents_own_folder(self):
        listing = {"terminals": [{"address": "t1", "agent": "antigravity", "state": "live", "cwd": "/home"}]}
        detail = {"cwd": "/home", "title": "", "foreground": {"pid": 7, "cwd": "/work"}}
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.run_json = lambda args: listing if args[1] == "ls" else detail
        self.assertEqual(module.get_app_terminals(),
                         [{"address": "t1", "agent": "antigravity", "pid": 7, "cwd": "/work", "title": ""}])

    def test_antigravity_is_read_next_to_claude(self):
        self.write(f".gemini/antigravity-cli/brain/{UUID_A}/.system_generated/logs/transcript.jsonl", [agy_turn(300_000)])
        self.terminals.append({"address": "t2", "agent": "antigravity", "pid": 202, "cwd": "/agy", "title": ""})
        lsof = mock.Mock(stdout=f"p202\nn/h/.gemini/antigravity-cli/conversations/{UUID_A}.db\n")
        with mock.patch.object(self.m.subprocess, "run", return_value=lsof):
            readings = self.poll()
        self.assertEqual(readings, {"t1": {"pct": 25, "used": 50_000, "window": 200_000},
                                    "t2": {"pct": 30, "used": 300_000, "window": 1_000_000}})

    def test_claude_session_comes_from_its_pid_file(self):
        self.assertEqual(self.m.claude_session_id(101), "s1")
        self.assertIsNone(self.m.claude_session_id(999))

    def test_codex_session_comes_from_the_one_rollout_its_process_has_open(self):
        one = "p7\nn/h/.codex/sessions/2026/11/20/rollout-2026-11-20T10-14-34-01a1f873-632b-7b62-811d-e1c313203a13.jsonl\n"
        other = "n/h/.codex/sessions/2026/11/20/rollout-2026-11-20T10-16-11-01a1f874-ddf3-7712-812e-8eba89cae628.jsonl\n"
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout=one)):
            self.assertEqual(self.m.codex_session_id(7), "01a1f873-632b-7b62-811d-e1c313203a13")
        # a shared daemon holds every session's rollout: no telling which is this terminal's
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout=one + other)):
            self.assertIsNone(self.m.codex_session_id(7))
        with mock.patch.object(self.m.subprocess, "run", side_effect=OSError):
            self.assertIsNone(self.m.codex_session_id(7))

    def test_readings_are_keyed_by_terminal_and_unknown_ones_left_out(self):
        self.terminals.append({"address": "t2", "agent": "claude", "pid": 102, "cwd": "/work", "title": ""})
        self.assertEqual(self.poll(), {"t1": {"pct": 25, "used": 50_000, "window": 200_000}})

    def test_a_later_run_reads_only_what_the_transcript_gained(self):
        self.poll()
        with open(self.transcript, "a") as f:
            f.write(json.dumps(assistant(100_000)) + "\n" + '{"type": "assist')
        later = self.fresh_module()
        self.assertEqual(self.poll(later)["t1"]["pct"], 50)
        self.assertEqual(later._tails[self.transcript]["offset"], self.transcript.stat().st_size)
        # the unfinished line is kept from one run to the next and completed by what follows
        with open(self.transcript, "a") as f:
            f.write('ant", "message": {"usage": {"input_tokens": 150000}}}\n')
        self.assertEqual(self.poll(self.fresh_module())["t1"]["pct"], 75)

    def test_an_unreadable_state_file_starts_over(self):
        self.state.write_text("{not json")
        self.assertEqual(self.poll()["t1"]["pct"], 25)
        self.assertIn(str(self.transcript), json.loads(self.state.read_text())["tails"])

    def test_main_uses_the_config_next_to_the_monitor(self):
        self.config_dir.mkdir()
        (self.config_dir / "config.json").write_text(json.dumps({"context_windows": {"claude-sonnet": 500_000}}))
        with mock.patch.object(self.m, "__file__", str(self.config_dir / "monitor.py")):
            readings = self.poll()
        self.assertEqual(readings["t1"], {"pct": 10, "used": 50_000, "window": 500_000})

    def test_the_installed_mods_poll_command_still_works(self):
        self.config_dir.mkdir()
        (self.config_dir / "config.json").write_text(json.dumps({"context_windows": {"claude-sonnet": 1_000_000}}))
        readings = self.poll(args=("--app", "--config-dir", str(self.config_dir)))
        self.assertEqual(readings["t1"], {"pct": 5, "used": 50_000, "window": 1_000_000})

    def test_process_start_time_parses_ps_lstart(self):
        expected = time.mktime(time.strptime("Mon Nov  2 12:12:30 2026", "%a %b %d %H:%M:%S %Y"))
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="Mon Nov  2 12:12:30 2026\n")):
            self.assertEqual(self.m.process_start_time(123), expected)
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(self.m.process_start_time(123))
        with mock.patch.object(self.m.subprocess, "run", side_effect=OSError):
            self.assertIsNone(self.m.process_start_time(123))

    def test_new_session_does_not_inherit_a_finished_sessions_reading(self):
        old = self.write(f".gemini/antigravity-cli/brain/{UUID_A}/.system_generated/logs/transcript.jsonl", [agy_turn(530_000)])
        self.write(".gemini/antigravity-cli/cache/last_conversations.json", [])
        (self.home / ".gemini/antigravity-cli/cache/last_conversations.json").write_text(json.dumps({"/agy": UUID_A}))
        now = time.time()
        os.utime(old, (now - 30, now - 30))
        self.terminals = [{"address": "t2", "agent": "antigravity", "pid": 202, "cwd": "/agy", "title": ""}]
        with mock.patch.object(self.m, "antigravity_session_id", return_value=None), \
             mock.patch.object(self.m, "process_start_time", return_value=now):
            self.assertEqual(self.poll(), {})


if __name__ == "__main__":
    unittest.main()
