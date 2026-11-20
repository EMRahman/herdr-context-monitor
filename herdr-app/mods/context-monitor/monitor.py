#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

# The Herdr desktop app's terminals live in herdr-core, which `hc` drives.
HC_BIN = shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")
# How far each transcript has been read is cached between polls.
APP_STATE = Path.home() / ".cache/herdr-context-monitor/app-state.json"
# herdr-core's name for each agent (`hc ls`), as this monitor names it.
APP_AGENTS = {"claude-code": "claude", "claude": "claude", "codex": "codex"}
# A Codex rollout file, named after its session id.
ROLLOUT = re.compile(r"/rollout-[^/]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")

# Context window sizes (tokens) used when a transcript does not state its own.
CLAUDE_STANDARD_WINDOW = 200_000
CLAUDE_LONG_WINDOW = 1_000_000    # a request past 200k can only have run in the 1M window

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# The summary line Claude Code's /context prints, e.g. "107.9k/1m tokens (11%)"
CONTEXT_OUTPUT = re.compile(r"(\d+(?:\.\d+)?[km]?)\s*/\s*(\d+(?:\.\d+)?[km]?)\s*tokens\s*\(", re.IGNORECASE)

# Per transcript: how far it has been read, and what the parser has gathered so far.
_tails = {}
# Transcripts read during the current poll; the others are forgotten after it.
_touched = set()
# Session id -> transcript path, so a session is searched for only once.
_paths = {}
# Codex rollout path -> its session_meta, which never changes once written.
_codex_meta = {}


def load_context_windows(config_dir):
    """Reads {"context_windows": {"<model id prefix>": <tokens>}} from the mod's config.json."""
    try:
        with open(config_dir / "config.json", "r", encoding="utf-8") as f:
            windows = json.load(f).get("context_windows", {})
        return {str(k): int(v) for k, v in windows.items() if int(v) > 0}
    except Exception:
        return {}


def configured_window(windows, model):
    """The window set for the longest model id prefix that matches, if any."""
    if not model:
        return None
    matches = [k for k in windows if model.startswith(k)]
    return windows[max(matches, key=len)] if matches else None


def parse_count(text):
    """Turns "107.9k" or "1m" into a token count."""
    text = text.lower()
    scale = 1_000 if text.endswith("k") else 1_000_000 if text.endswith("m") else 1
    return int(float(text.rstrip("km")) * scale)


def percent(tokens, window):
    if not tokens or not window:
        return None
    return min(100, round(tokens * 100 / window))


def reading(tokens, window):
    """What the pane reports: the percentage, and the token counts behind it; None when unknown."""
    pct = percent(tokens, window)
    return None if pct is None else {"pct": pct, "used": int(tokens), "window": int(window)}


def read_new_entries(path, parse, initial):
    """Feeds the JSON lines appended to path since the last poll to parse(state, entry); returns the state."""
    st = path.stat()
    _touched.add(path)
    tail = _tails.get(path)
    if tail is None or st.st_ino != tail["inode"] or st.st_size < tail["offset"]:
        tail = {"inode": st.st_ino, "offset": 0, "rest": b"", "state": initial()}
        _tails[path] = tail
    if st.st_size > tail["offset"]:
        with open(path, "rb") as f:
            f.seek(tail["offset"])
            data = f.read()
        tail["offset"] += len(data)
        lines = (tail["rest"] + data).split(b"\n")
        # an unfinished last line waits for the rest of it
        tail["rest"] = lines.pop()
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                parse(tail["state"], entry)
    return tail["state"]


def entry_text(entry):
    content = entry.get("content")
    if content is None and isinstance(entry.get("message"), dict):
        content = entry["message"].get("content")
    if isinstance(content, list):
        return " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    return content if isinstance(content, str) else ""


def parse_claude(state, entry):
    if entry.get("type") == "assistant":
        # a subagent's request runs on its own model and fills its own context, not this one's
        if entry.get("isSidechain"):
            return
        message = entry.get("message") or {}
        model = message.get("model")
        if model and not model.startswith("<"):
            if state["model"] and model != state["model"]:
                # the window /context reported, and the peak that implies 1M, belong to the model they ran under
                state["window"] = None
                state["peak"] = 0
            state["model"] = model
        usage = message.get("usage")
        if usage:
            tokens = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            if tokens:
                state["tokens"] = tokens
                state["peak"] = max(state["peak"], tokens)
        return
    # /context output states the use and the window; matching only command output keeps chat text from posing as it
    text = entry_text(entry)
    if "<local-command-stdout>" in text:
        m = CONTEXT_OUTPUT.search(ANSI.sub("", text))
        if m:
            state["tokens"] = parse_count(m.group(1))
            state["window"] = parse_count(m.group(2))


def claude_project_dir(cwd):
    # Claude Code names a project's folder after its path, with every non-alphanumeric character as "-"
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def find_claude_transcript(session_id, cwd, may_guess, claimed):
    if session_id:
        cached = _paths.get(session_id)
        if cached and cached.exists():
            return cached
        candidates = [claude_project_dir(cwd) / f"{session_id}.jsonl"] if cwd else []
        candidates += (Path.home() / ".claude/projects").glob(f"*/{session_id}.jsonl")
        for candidate in candidates:
            if candidate.exists():
                _paths[session_id] = candidate
                return candidate
        return None
    # Without a session id, the newest unclaimed transcript is only a safe guess for the one Claude pane in this folder
    if may_guess and cwd:
        project_dir = claude_project_dir(cwd)
        if project_dir.is_dir():
            unclaimed = [p for p in project_dir.glob("*.jsonl") if p.stem not in claimed]
            return max(unclaimed, key=lambda p: p.stat().st_mtime, default=None)
    return None


def get_claude_context(session_id, cwd, may_guess, claimed, windows):
    """Inspects Claude Code project session transcripts."""
    path = find_claude_transcript(session_id, cwd, may_guess, claimed)
    if path is None:
        return None
    state = read_new_entries(path, parse_claude, lambda: {"tokens": None, "peak": 0, "model": None, "window": None})
    window = (
        state["window"]
        or configured_window(windows, state["model"])
        or (CLAUDE_LONG_WINDOW if state["peak"] > CLAUDE_STANDARD_WINDOW else CLAUDE_STANDARD_WINDOW)
    )
    return reading(state["tokens"], window)


def parse_codex(state, entry):
    payload = entry.get("payload") or {}
    if entry.get("type") == "event_msg" and payload.get("type") == "token_count":
        # what the last request filled, against the window Codex itself states
        info = payload.get("info") or {}
        tokens = (info.get("last_token_usage") or {}).get("total_tokens")
        if tokens:
            state["tokens"] = tokens
        if info.get("model_context_window"):
            state["window"] = info["model_context_window"]
    elif entry.get("type") == "turn_context" and payload.get("model"):
        if state["model"] and payload["model"] != state["model"]:
            # the stated window belongs to the old model; a configured one applies until Codex states the new one
            state["window"] = None
        state["model"] = payload["model"]


def codex_session_meta(path):
    if path in _codex_meta:
        return _codex_meta[path]
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.loads(f.readline())
    except Exception:
        # a rollout still being created is read again on the next poll
        return {}
    payload = entry.get("payload") if entry.get("type") == "session_meta" else None
    meta = {"cwd": payload.get("cwd"), "id": payload.get("id")} if isinstance(payload, dict) else {}
    _codex_meta[path] = meta
    return meta


def find_codex_transcript(session_id, cwd, title, may_guess, claimed):
    codex_dir = Path.home() / ".codex"
    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.exists():
        return None
    if session_id:
        cached = _paths.get(session_id)
        if cached and cached.exists():
            return cached
        for candidate in sessions_dir.rglob(f"*{session_id}.jsonl"):
            _paths[session_id] = candidate
            return candidate
        return None
    if not may_guess:
        return None
    # Without a session id, guess only for the one Codex pane in this folder, from sessions no other pane claims
    rollouts = sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if title:
        index_file = codex_dir / "session_index.jsonl"
        if index_file.exists():
            matching_ids = set()
            with open(index_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    t_name = d.get("thread_name")
                    if t_name and d.get("id") and d["id"] not in claimed and (t_name in title or title in t_name):
                        matching_ids.add(d["id"])
            matches = [candidate for candidate in rollouts
                       if (meta := codex_session_meta(candidate)).get("cwd") == cwd
                       and meta.get("id") in matching_ids]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
    # each rollout's first line is read once, so searching all of them stays cheap
    for candidate in rollouts:
        meta = codex_session_meta(candidate)
        if meta.get("cwd") == cwd and meta.get("id") not in claimed:
            return candidate
    return None


def get_codex_context(session_id, cwd, title, may_guess, claimed, windows):
    """Inspects OpenAI Codex session usage."""
    path = find_codex_transcript(session_id, cwd, title, may_guess, claimed)
    if path is None:
        return None
    state = read_new_entries(path, parse_codex, lambda: {"tokens": None, "model": None, "window": None})
    # no stated or configured window means no reading, rather than one against a guess
    return reading(state["tokens"], state["window"] or configured_window(windows, state["model"]))


def run_json(args):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return json.loads(res.stdout)
    except Exception:
        return None


def get_app_terminals():
    """The Herdr app's live agent terminals, from herdr-core: what each runs, its pid, folder and title."""
    terminals = []
    for t in (run_json([HC_BIN, "ls", "--json"]) or {}).get("terminals", []):
        agent = APP_AGENTS.get(t.get("agent"))
        if t.get("state") != "live" or not agent or not t.get("address"):
            continue
        detail = run_json([HC_BIN, "get", t["address"], "--json"]) or {}
        # the agent runs in the terminal's shell: its own folder can differ from where the shell started
        foreground = detail.get("foreground") or {}
        terminals.append({
            "address": t["address"],
            "agent": agent,
            "pid": foreground.get("pid"),
            "cwd": foreground.get("cwd") or detail.get("cwd") or t.get("cwd") or "",
            "title": detail.get("title") or "",
        })
    return terminals


def claude_session_id(pid):
    """Claude Code records each running process's session in ~/.claude/sessions/<pid>.json."""
    try:
        with open(Path.home() / ".claude/sessions" / f"{int(pid)}.json", "r", encoding="utf-8") as f:
            return json.load(f).get("sessionId") or None
    except Exception:
        return None


def codex_session_id(pid):
    """The session whose rollout this Codex process has open; None when it has none or several (a shared daemon)."""
    try:
        res = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    ids = {m.group(1) for line in res.stdout.splitlines() if line.startswith("n") and (m := ROLLOUT.search(line))}
    return ids.pop() if len(ids) == 1 else None


def app_readings(windows):
    """Readings for the Herdr app's agent terminals, by terminal address; unknown ones are left out."""
    terminals = get_app_terminals()
    for t in terminals:
        find_session = claude_session_id if t["agent"] == "claude" else codex_session_id
        t["session"] = find_session(t["pid"]) if t["pid"] else None
    claimed = {t["session"] for t in terminals if t["session"]}
    per_folder = Counter((t["agent"], t["cwd"]) for t in terminals)
    readings = {}
    for t in terminals:
        may_guess = per_folder[(t["agent"], t["cwd"])] == 1
        try:
            if t["agent"] == "claude":
                current = get_claude_context(t["session"], t["cwd"], may_guess, claimed, windows)
            else:
                current = get_codex_context(t["session"], t["cwd"], t["title"], may_guess, claimed, windows)
        except Exception:
            current = None
        if current is not None:
            readings[t["address"]] = current
    for path in set(_tails) - _touched:
        del _tails[path]
    _touched.clear()
    return readings


def load_app_state(path):
    """Picks up where the last poll stopped reading, so each poll reads only what transcripts gained."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tails = {Path(p): {"inode": t["inode"], "offset": t["offset"], "rest": t["rest"].encode("latin-1"),
                           "state": t["state"]} for p, t in data["tails"].items()}
        paths = {k: Path(v) for k, v in data["paths"].items()}
        codex_meta = {Path(p): m for p, m in data["codex_meta"].items()}
    except Exception:
        return
    _tails.update(tails)
    _paths.update(paths)
    _codex_meta.update(codex_meta)


def save_app_state(path):
    data = {
        "tails": {str(p): {"inode": t["inode"], "offset": t["offset"], "rest": t["rest"].decode("latin-1"),
                           "state": t["state"]} for p, t in _tails.items()},
        "paths": {k: str(v) for k, v in _paths.items()},
        "codex_meta": {str(p): m for p, m in _codex_meta.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # written aside and moved into place, so a run starting meanwhile never reads half a file
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def main(argv=None):
    """For the Herdr app's mod: one reading of every agent terminal, printed as JSON."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    # Keep the existing desktop mod invocation working during an update.
    parser.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--state", type=Path, default=APP_STATE)
    args = parser.parse_args(argv)
    load_app_state(args.state)
    readings = app_readings(load_context_windows(args.config_dir))
    save_app_state(args.state)
    print(json.dumps({"readings": readings}))


if __name__ == "__main__":
    main()
