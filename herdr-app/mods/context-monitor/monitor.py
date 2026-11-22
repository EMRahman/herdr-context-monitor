#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# The Herdr desktop app's terminals live in herdr-core, which `hc` drives.
HC_BIN = shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")
# How far each transcript has been read is cached between polls.
APP_STATE = Path.home() / ".cache/herdr-context-monitor/app-state.json"
CATALOG_REFRESH_SECONDS = 60 * 60
# herdr-core's name for each agent (`hc ls`), as this monitor names it.
APP_AGENTS = {"claude-code": "claude", "claude": "claude", "codex": "codex",
              "antigravity": "antigravity", "agy": "antigravity"}
# A Codex rollout file, named after its session id.
ROLLOUT = re.compile(r"/rollout-[^/]*?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# An Antigravity conversation's database, named after its conversation id.
AGY_CONVERSATION = re.compile(r"/conversations/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.db$")

# Context window sizes (tokens) used when a transcript does not state its own.
CLAUDE_STANDARD_WINDOW = 200_000
CLAUDE_LONG_WINDOW = 1_000_000    # a request past 200k can only have run in the 1M window
GEMINI_WINDOW = 1_000_000         # Antigravity's transcripts never state a window; Gemini's is 1M

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
# File signatures and extracted limits survive the one-shot Python polls.
_catalog_files = {}
_model_windows = {"claude": {}, "codex": {}, "antigravity": {}}


def catalog_entries(agent, data):
    """Extract only model IDs and usable context sizes from a provider's local catalog."""
    if agent == "claude":
        rows = data["catalog"]["config"]["models"]
    elif agent == "codex":
        rows = data["models"]
    elif agent == "antigravity-cli":
        custom = data.get("customModelsConfig", {}).get("customModels", {})
        rows = [dict(row, modelId=row.get("modelId") or key)
                for key, row in custom.items() if isinstance(row, dict)]
    else:
        rows = data.get("models", {}).get("list")
        if rows is None:
            return None  # no local model list at this settings level
    if not isinstance(rows, list):
        raise ValueError("expected a model list")
    windows = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if agent == "claude":
            model = row.get("id")
            runtime = row.get("runtime")
            window = runtime.get("max_input_tokens") if isinstance(runtime, dict) else None
        elif agent == "codex":
            model = row.get("slug")
            window = row.get("context_window")
            effective = row.get("effective_context_window_percent", 95)
            if type(window) is not int or type(effective) is not int or not 0 < effective <= 100:
                continue
            window = window * effective // 100
        else:
            model = row.get("modelId")
            window = row.get("maxTokens")
        if isinstance(model, str) and model and type(window) is int and window > 0:
            windows[model] = window
    return windows


def cached_catalog(agent, path):
    key = str(path)
    previous = _catalog_files.get(key, {})
    try:
        st = path.stat()
        signature = [st.st_ino, st.st_size, st.st_mtime_ns]
        if previous.get("signature") == signature:
            return previous["windows"]
        windows = catalog_entries(agent, json.loads(path.read_bytes()))
        _catalog_files[key] = {"signature": signature, "windows": windows}
        return windows
    except FileNotFoundError:
        _catalog_files.pop(key, None)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Keep the last readable sizes until the next catalog check.
        return previous.get("windows")
    return None


def refresh_model_windows():
    home = Path.home()
    claude_dir = home / ".claude/cache/model-catalog"
    claude_files = sorted(claude_dir.glob("*-cc.json"))
    # Different Claude accounts may have different limits for the same ID.
    # Use only limits their catalogs agree on when the account is ambiguous.
    sizes = {}
    for path in claude_files:
        for model, window in (cached_catalog("claude", path) or {}).items():
            sizes.setdefault(model, set()).add(window)
    _model_windows["claude"] = {m: next(iter(limits)) for m, limits in sizes.items() if len(limits) == 1}
    for key in list(_catalog_files):
        if Path(key).parent == claude_dir and Path(key) not in claude_files:
            del _catalog_files[key]

    _model_windows["codex"] = cached_catalog("codex", home / ".codex/models_cache.json") or {}
    custom = cached_catalog("antigravity-cli", home / ".gemini/antigravity-cli/settings.json") or {}
    admin = (Path("/Library/Application Support/Antigravity") if sys.platform == "darwin"
             else Path("/etc/antigravity")) / "admin_settings.json"
    # A managed list replaces the user list as a whole, including an empty list.
    for path in (admin, home / ".gemini/config/config.json", home / ".antigravity/settings.json"):
        windows = cached_catalog("antigravity", path)
        if windows is not None:
            _model_windows["antigravity"] = {**custom, **windows}
            break
    else:
        _model_windows["antigravity"] = custom


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
            long_variant = str(entry.get("requestedModel", "")).endswith("[1m]")
            if state["model"] and (model != state["model"] or long_variant != state.get("long_variant", False)):
                # the window /context reported, and the peak that implies 1M, belong to the model they ran under
                state["window"] = None
                state["peak"] = 0
            state["model"] = model
            state["long_variant"] = long_variant
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


def find_claude_transcript(session_id, cwd, may_guess, claimed, started_at=None):
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
            unclaimed = [
                p for p in project_dir.glob("*.jsonl")
                if p.stem not in claimed and (started_at is None or p.stat().st_mtime >= started_at)
            ]
            return max(unclaimed, key=lambda p: p.stat().st_mtime, default=None)
    return None


def get_claude_context(session_id, cwd, may_guess, claimed, windows, started_at=None):
    """Inspects Claude Code project session transcripts."""
    path = find_claude_transcript(session_id, cwd, may_guess, claimed, started_at)
    if path is None:
        return None
    state = read_new_entries(path, parse_claude, lambda: {"tokens": None, "peak": 0, "model": None, "window": None})
    model = state["model"]
    catalog_window = _model_windows["claude"].get(model)
    # For 4.6 the catalog advertises the opt-in maximum even on a standard session.
    if state.get("long_variant"):
        catalog_window = CLAUDE_LONG_WINDOW
    elif model in ("claude-opus-4-6", "claude-sonnet-4-6") or (catalog_window and catalog_window < state["peak"]):
        catalog_window = None
    window = (
        state["window"]
        or configured_window(windows, model)
        or catalog_window
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


def find_codex_transcript(session_id, cwd, title, may_guess, claimed, started_at=None):
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
    rollouts = [
        p for p in sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if started_at is None or p.stat().st_mtime >= started_at
    ]
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


def get_codex_context(session_id, cwd, title, may_guess, claimed, windows, started_at=None):
    """Inspects OpenAI Codex session usage."""
    path = find_codex_transcript(session_id, cwd, title, may_guess, claimed, started_at)
    if path is None:
        return None
    state = read_new_entries(path, parse_codex, lambda: {"tokens": None, "model": None, "window": None})
    window = state["window"] or configured_window(windows, state["model"]) or _model_windows["codex"].get(state["model"])
    return reading(state["tokens"], window)


def parse_antigravity(state, entry):
    tokens = entry.get("input_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        return
    # a conversation can alternate models over one shared context: the window and peak carry across them
    if entry.get("model"):
        state["model"] = entry["model"]
    tokens += entry.get("cache_read_tokens") or 0
    if tokens > 0:
        state["tokens"] = tokens
        state["peak"] = max(state["peak"], tokens)


def antigravity_last_conversations(cwd):
    """The conversations Antigravity last ran in this folder, from every ~/.gemini/*/cache/last_conversations.json."""
    ids = set()
    if not cwd:
        return ids
    for index in sorted((Path.home() / ".gemini").glob("*/cache/last_conversations.json")):
        try:
            with open(index, "r", encoding="utf-8") as f:
                conversation = json.load(f).get(cwd)
        except Exception:
            continue
        if isinstance(conversation, str):
            ids.add(conversation)
    return ids


def antigravity_transcript_path(session_id):
    for candidate in sorted((Path.home() / ".gemini").glob(f"*/brain/{session_id}/.system_generated/logs/transcript.jsonl")):
        if candidate.exists():
            return candidate
    return None


def find_antigravity_transcript(session_id, cwd, may_guess, claimed, started_at=None):
    if session_id:
        cached = _paths.get(session_id)
        if cached and cached.exists():
            return cached
        path = antigravity_transcript_path(session_id)
        if path is not None:
            _paths[session_id] = path
        return path
    # Without a session id, only the conversation last run in this folder is a safe guess for its one pane
    if may_guess and cwd:
        found = [
            p for p in (antigravity_transcript_path(i) for i in antigravity_last_conversations(cwd) - claimed)
            if p and (started_at is None or p.stat().st_mtime >= started_at)
        ]
        if len(found) == 1:
            return found[0]
    return None


def get_antigravity_context(session_id, cwd, may_guess, claimed, windows, started_at=None):
    """Inspects Antigravity conversation transcripts."""
    path = find_antigravity_transcript(session_id, cwd, may_guess, claimed, started_at)
    if path is None:
        return None
    state = read_new_entries(path, parse_antigravity, lambda: {"tokens": None, "peak": 0, "model": None})
    model = state["model"] or ""
    window = (
        configured_window(windows, model)
        or _model_windows["antigravity"].get(model)
        or (GEMINI_WINDOW if model.startswith("gemini") else None)
        or (CLAUDE_LONG_WINDOW if state["peak"] > CLAUDE_STANDARD_WINDOW else CLAUDE_STANDARD_WINDOW)
    )
    return reading(state["tokens"], window)


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


def antigravity_session_id(pid, cwd):
    """The conversation whose database this Antigravity process has open; with several, the one last run in its folder."""
    try:
        res = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    ids = {m.group(1) for line in res.stdout.splitlines() if line.startswith("n") and (m := AGY_CONVERSATION.search(line))}
    if len(ids) > 1:
        ids &= antigravity_last_conversations(cwd)
    return ids.pop() if len(ids) == 1 else None


def process_start_time(pid):
    """When this process started (Unix seconds), or None if unknown."""
    try:
        env = {**os.environ, "LC_ALL": "C"}
        res = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5, env=env)
        text = res.stdout.strip()
        if res.returncode != 0 or not text:
            return None
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except Exception:
        return None


def app_readings(windows):
    """Readings for the Herdr app's agent terminals, by terminal address; unknown ones are left out."""
    terminals = get_app_terminals()
    for t in terminals:
        t["session"] = None
        if not t["pid"]:
            continue
        if t["agent"] == "claude":
            t["session"] = claude_session_id(t["pid"])
        elif t["agent"] == "codex":
            t["session"] = codex_session_id(t["pid"])
        else:
            t["session"] = antigravity_session_id(t["pid"], t["cwd"])
    claimed = {t["session"] for t in terminals if t["session"]}
    per_folder = Counter((t["agent"], t["cwd"]) for t in terminals)
    readings = {}
    for t in terminals:
        may_guess = per_folder[(t["agent"], t["cwd"])] == 1
        started_at = process_start_time(t["pid"]) if (not t["session"] and may_guess and t["pid"]) else None
        try:
            if t["agent"] == "claude":
                current = get_claude_context(t["session"], t["cwd"], may_guess, claimed, windows, started_at)
            elif t["agent"] == "codex":
                current = get_codex_context(t["session"], t["cwd"], t["title"], may_guess, claimed, windows, started_at)
            else:
                current = get_antigravity_context(t["session"], t["cwd"], may_guess, claimed, windows, started_at)
        except Exception:
            current = None
        if current is not None:
            readings[t["address"]] = current
    for path in set(_tails) - _touched:
        del _tails[path]
    _touched.clear()
    return readings


def load_app_state(path):
    """Restore transcript offsets and return the time of the last usable catalog check."""
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
    if isinstance(data.get("catalog_files"), dict):
        _catalog_files.update({k: v for k, v in data["catalog_files"].items() if isinstance(v, dict)})
    windows = data.get("model_windows")
    checked_at = data.get("catalog_checked_at")
    if isinstance(windows, dict) and type(checked_at) in (int, float):
        if all(isinstance(windows.get(agent), dict) and
               all(type(n) is int and n > 0 for n in windows[agent].values()) for agent in _model_windows):
            _model_windows.update({agent: windows[agent] for agent in _model_windows})
            return checked_at


def save_app_state(path, catalog_checked_at):
    data = {
        "tails": {str(p): {"inode": t["inode"], "offset": t["offset"], "rest": t["rest"].decode("latin-1"),
                           "state": t["state"]} for p, t in _tails.items()},
        "paths": {k: str(v) for k, v in _paths.items()},
        "codex_meta": {str(p): m for p, m in _codex_meta.items()},
        "catalog_files": _catalog_files,
        "model_windows": _model_windows,
        "catalog_checked_at": catalog_checked_at,
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
    checked_at = load_app_state(args.state)
    now = time.time()
    if checked_at is None or not 0 <= now - checked_at < CATALOG_REFRESH_SECONDS:
        refresh_model_windows()
        checked_at = now
    readings = app_readings(load_context_windows(args.config_dir))
    save_app_state(args.state, checked_at)
    print(json.dumps({"readings": readings}))


if __name__ == "__main__":
    main()
