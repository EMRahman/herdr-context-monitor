#!/usr/bin/env python3
import json
import os
import re
import sys
import subprocess
import time
from pathlib import Path

# Resolve herdr binary
HERDR_BIN = os.environ.get("HERDR_BIN_PATH")
if not HERDR_BIN or not os.path.exists(HERDR_BIN):
    default_bin = os.path.expanduser("~/.local/bin/herdr")
    HERDR_BIN = default_bin if os.path.exists(default_bin) else "herdr"

# Context window limits (tokens)
WINDOW_LIMITS = {
    "agy": 1_000_000,    # Gemini Pro / Flash 1M window
    "claude": 200_000,   # Claude 3.5 / 3.7 Sonnet 200k window
    "codex": 128_000,    # OpenAI Codex default 128k window
}

def get_open_panes():
    try:
        res = subprocess.run([HERDR_BIN, "pane", "list"], capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        return data.get("result", {}).get("panes", [])
    except Exception:
        return []

def get_agy_context_percent(session_id):
    """Inspects Antigravity CLI's transcript logs."""
    if not session_id:
        return None
    log_path = Path.home() / f".gemini/antigravity-cli/brain/{session_id}/.system_generated/logs/transcript.jsonl"
    if not log_path.exists():
        return None
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in reversed(f.readlines()):
                try:
                    entry = json.loads(line)
                    in_tok = entry.get("input_tokens", 0)
                    cache_tok = entry.get("cache_read_tokens", 0)
                    if in_tok or cache_tok:
                        total = in_tok + cache_tok
                        return min(100, round((total / WINDOW_LIMITS["agy"]) * 100))
                except Exception:
                    continue
    except Exception:
        pass
    return None

def get_claude_context_percent(session_id, cwd):
    """Inspects Claude Code project session transcripts."""
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return None
    try:
        session_file = None
        # Try finding in projects by cwd slug
        if cwd:
            proj_slug = "-" + str(cwd).strip("/").replace("/", "-")
            proj_dir = claude_dir / "projects" / proj_slug
            if proj_dir.exists():
                if session_id:
                    candidate = proj_dir / f"{session_id}.jsonl"
                    if candidate.exists():
                        session_file = candidate
                if not session_file:
                    # Pick newest jsonl in project directory
                    jsonls = list(proj_dir.glob("*.jsonl"))
                    if jsonls:
                        jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                        session_file = jsonls[0]

        # Fallback: scan all project files for session_id
        if not session_file and session_id:
            for p in (claude_dir / "projects").glob(f"**/{session_id}.jsonl"):
                session_file = p
                break

        if session_file and session_file.exists():
            with open(session_file, "r", encoding="utf-8") as f:
                for line in reversed(f.readlines()):
                    try:
                        entry = json.loads(line)
                        usage = entry.get("message", {}).get("usage") or entry.get("usage")
                        if usage:
                            tokens = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                            )
                            return min(100, round((tokens / WINDOW_LIMITS["claude"]) * 100))

                        # Check local command output or message text (e.g. from /context)
                        text = ""
                        if "content" in entry:
                            c = entry["content"]
                            text = c if isinstance(c, str) else " ".join(item.get("text", "") for item in c if isinstance(item, dict))
                        elif "message" in entry and isinstance(entry["message"], dict):
                            c = entry["message"].get("content", "")
                            text = c if isinstance(c, str) else " ".join(item.get("text", "") for item in c if isinstance(item, dict))

                        if text:
                            m = re.search(r"(\d+(?:\.\d+)?k?)\s*/\s*(\d+(?:\.\d+)?m?)\s*tokens\s*\(([0-9]+)%\)", text, re.IGNORECASE)
                            if m:
                                return int(m.group(3))
                            m2 = re.search(r"tokens\s*\(([0-9]+)%\)", text, re.IGNORECASE)
                            if m2:
                                return int(m2.group(1))
                    except Exception:
                        continue
    except Exception:
        pass
    return None

def get_codex_context_percent(session_id, cwd, title=None):
    """Inspects OpenAI Codex session usage."""
    codex_dir = Path.home() / ".codex"
    if not codex_dir.exists():
        return None
    try:
        matched_id = session_id
        if not matched_id and title:
            index_file = codex_dir / "session_index.jsonl"
            if index_file.exists():
                with open(index_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                            t_name = d.get("thread_name")
                            if t_name and (t_name in title or title in t_name):
                                matched_id = d.get("id")
                                break
                        except Exception:
                            continue

        target_file = None
        sessions_dir = codex_dir / "sessions"
        if matched_id and sessions_dir.exists():
            matched_files = list(sessions_dir.rglob(f"*{matched_id}*.jsonl"))
            if matched_files:
                target_file = matched_files[0]

        if not target_file and sessions_dir.exists():
            jsonls = list(sessions_dir.rglob("*.jsonl"))
            if jsonls:
                jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                target_file = jsonls[0]

        if target_file and target_file.exists():
            with open(target_file, "r", encoding="utf-8") as f:
                for line in reversed(f.readlines()):
                    if "token_usage_record" in line:
                        try:
                            entry = json.loads(line)
                            usage = entry.get("payload", {}).get("usage", {})
                            tokens = usage.get("total_tokens") or usage.get("input_tokens", 0)
                            if tokens:
                                return min(100, round((tokens / WINDOW_LIMITS["codex"]) * 100))
                        except Exception:
                            continue
    except Exception:
        pass
    return None

def report_metadata(pane_id, pct):
    subprocess.run([
        HERDR_BIN, "pane", "report-metadata", str(pane_id),
        "--source", "context-monitor",
        "--token", f"ctx_pct={int(pct)}"
    ], capture_output=True)

def update_all_panes():
    panes = get_open_panes()
    for pane in panes:
        pane_id = pane.get("pane_id")
        agent = pane.get("agent")
        session = pane.get("agent_session", {}) or {}
        session_id = session.get("value")
        cwd = pane.get("cwd", "")
        title = pane.get("terminal_title") or pane.get("title") or ""

        if not pane_id or not agent:
            continue

        pct = None
        if agent == "agy":
            pct = get_agy_context_percent(session_id)
        elif agent == "claude":
            pct = get_claude_context_percent(session_id, cwd)
        elif agent == "codex":
            pct = get_codex_context_percent(session_id, cwd, title)

        if pct is None and agent in ("agy", "claude", "codex"):
            pct = 0

        if pct is not None:
            report_metadata(pane_id, pct)

def main():
    once = "--once" in sys.argv
    while True:
        try:
            update_all_panes()
        except Exception:
            pass
        if once:
            break
        time.sleep(5)

if __name__ == "__main__":
    main()
