# herdr-context-monitor

Herdr plugin to monitor and report real-time context window usage for active AI agents (Claude Code, OpenAI Codex, Antigravity CLI).

## Features

- **Continuous Context Tracking**: Periodically inspects active session transcripts and computes percentage usage against model context limits.
  - **Antigravity CLI**: 1M token window limit
  - **Claude Code**: 200k / 1M token window limit (including local `/context` outputs)
  - **OpenAI Codex**: 128k / rollout token window limit
- **Herdr Integration**: Emits integer metrics (`ctx_pct=<N>`) via `herdr pane report-metadata`.
- **TUI Highlighting**: Supports conditional rules in `~/.config/herdr/config.toml` (e.g. bold red alert when context > 50%).
- **Web UI Badge**: Displays context pills in the Herdr Web UI sidebar agent roster.

## Installation

```bash
herdr plugin install github:EMRahman/herdr-context-monitor
```

Or link locally during development:

```bash
herdr plugin link /path/to/herdr-context-monitor
```

## TUI Configuration

Add to `~/.config/herdr/config.toml`:

```toml
[ui.sidebar.agents]
format = ["$mark", " ", "$title", " · ", "$cwd", " ", { token = "$ctx_pct", suffix = "%", rules = [{ gt = 50, fg = "#ef4444", bold = true }] }]
```

## Web UI Integration

The monitor emits token metadata to `pane.tokens.ctx_pct` via `herdr pane report-metadata`.

Visual badge support in the Herdr Web UI sidebar (rendering glanceable context pills with red warning alerts when context usage exceeds 50%) is implemented in [herdr-web-ui](https://github.com/EMRahman/herdr-web-ui) via branch [`feat/sidebar-context-badge`](https://github.com/EMRahman/herdr-web-ui/tree/feat/sidebar-context-badge).

