# Context Monitor

See which local Claude Code, Codex, or Antigravity sessions are filling up.
The fullest sessions appear first. Hover for token counts or click to jump to
the agent. Dock the panel under Agents or open it as a floating window.

![Context Monitor docked under the Agents list as one 32px Context Usage row of three pills; clicking the over-50% pill opens a list of those sessions, fullest first](preview.png)

## What you see

Dock the panel under the Agents list in your own Herdr app layout (see
**Built from**), or open it as a floating window.

- **A window**, 340px wide and as tall as its lines, in Herdr's own window frame
  (theme background, border, rounded corners, shadow). It opens where it was
  last dropped and comes back on the next start until closed.
- **Header**, 32px high with 16px side padding, like the sidebar's other
  sections: `Context Usage` (11.5px, medium, `dim`) and three database pills on
  one line, with no divider. Each pill holds a database icon, its session count
  (12px, medium, `foreground`; `dim` at zero) and its range in `dim`: `<25%`,
  `≤50%` (25% through 50%) and `>50%`. The last pill's icon and count turn red when it has readings,
  with a bold count. Click a populated pill to open its list in a popover.
  The floating window has a `×` on the right.
- **One line per agent in the open pill's list**, fullest first (highest percentage at
  the top; equal ones in `agents()` order: space, tab, pane), 28px tall, radius 8, inside 8px side padding, 9px gaps, 13px text:
  - the agent's program icon, 16px (tinted with `foreground` when the program
    table says `tint`);
  - its space name in `foreground` (at most 110px, cut with `…`), then
    `· <title>` in `dim`, filling the rest and cut with `…`. The title drops a
    leading status glyph or spinner and Codex's `[ ! ] ` / `[ . ] ` mark;
  - on the right, `⛁ 34%` at 12px in `dim`; above 50%, in `ansi1` and bold.
- **No readings yet**: the three counts are zero. Hover a pill for its range and
  the message explaining when agents appear.
- **Polling errors** replace the pills with a red status and show the error text
  on hover. When Python can't start or macOS asks to install the developer tools,
  click `Retry` (or `!` in the rail) to resume polling.
- An agent with no reading has no line: never a guessed or `0%` value.

## How it behaves

- Every 5 seconds, while at least one agent is on this computer, it runs
  the `monitor.py` shipped with the mod and redraws only when a reading or the
  error changes.
- Agent status (working, blocked, done, idle) doesn't change a line's look; the
  focused agent's line has `selected` behind it, and `hover` under the pointer.
- **Hover** a line: a tooltip `Context 66% left · 68k of 200k tokens` (counts as
  `k`, or `M` from a million).
- **Click** a line: close the popover and run `pane.focus {pane}` for that agent.
- **Drag** the window by its header.
- Lines appear and go as agents start, end or first answer.
- **Docked under Agents**: the 32px header uses the sidebar's `chrome` and a top
  `border`. The title shows only where it fits whole
  beside the pills (both measured with `on_size_change`); otherwise the pills
  are centred alone. Too narrow for those, a rail of pills
  show the database icon and count, with the range in the tooltip. Agent lists
  scroll in the popover.
- **When Herdr updates its `main` layout** and the panel was docked by the
  repository's `herdr-app/dock.py` (below), the docked copy notices at start and
  posts one info toast per new `main`: "Herdr updated its main layout",
  "Re-run dock.py to update main-context". It only reads; it never changes a
  layout.

## Built from

- `mods/context-monitor/mod.json`: `"place": "none"` and `"overlay": { "kind":
  "window", "key": "context-monitor", "keep": true, "width": 340 }`, so
  `herdr-app do mod.open --name context-monitor` opens (and again, closes) it as a
  kept window.
- **Docked under Agents**: Herdr's shipped layouts are read-only, so the block
  goes in the person's own layout (for a shipped one, a clone first:
  `herdr-app do layout.clone '{"name":"main","as":"main-context"}'`), right after
  the block whose `content.mod` is `agents`, in the same column:
  `{ "id": "context", "type": "block", "size": { "px": 32 }, "min": 32, "content": { "mod": "context-monitor" } }`.
- `main.js`: one `View`. `init` starts `cx.timer.every(5000)` (not during
  `checking()`); the poll runs `exec("python3", …, { timeout: 20000 })` in
  `cx.spawn` and keeps `{ address: { pct, used, window } }`. `render` reads
  `agents()`, `theme()` and `props().overlay` (window or block), and uses
  `run("pane.focus")`, `closeOverlay`, `.tooltip`, `.on_size_change`. A docked
  copy reads `mods/context-monitor/.dock.json` (written by `dock.py`, if used)
  and `layouts/main.json` with `readTextFile`; when the shown layout
  (`shared("layout")`) is the one `dock.py` built and `main.json` differs from
  the `main_text` it recorded, it calls `notify`, remembering the new `main`'s
  fingerprint in `localStorage`.
- `monitor.py` prints a snapshot of readings as JSON and exits. It reads
  terminals from herdr-core (`hc ls --json`, then
  `hc get <t> --json` for each `claude-code`, `codex` or `antigravity` terminal: its foreground
  process's pid and folder, and its title), then finds each session:
  - Claude Code: `~/.claude/sessions/<pid>.json` names the session; its
    transcript is `~/.claude/projects/*/<session>.jsonl`.
  - Codex: the one rollout the process has open (`lsof -p <pid>`). If a shared
    daemon holds several, the monitor matches by title and working directory
    using `~/.codex/session_index.jsonl` and each rollout's first line.
  - Antigravity: the conversation whose `~/.gemini/*/conversations/<id>.db` the
    process has open (`lsof -p <pid>`; with several, the one in
    `~/.gemini/*/cache/last_conversations.json` for its folder). Its transcript
    is `~/.gemini/*/brain/<id>/.system_generated/logs/transcript.jsonl`; use is
    each model turn's `input_tokens + cache_read_tokens`. The window comes from
    a configured override or its local gateway or CLI custom model list.
    The fallback is 1M for `gemini*` models, else 200k (1M past 200k).
  - Claude's window comes from `/context`, then `context_windows` in the mod's
    `config.json`, then the local Claude CLI model catalog, then 1M past
    200k, else 200k. Codex uses `model_context_window` in its transcript,
    a configured window or its local catalog's effective context window.
    The first poll checks all three agents' local catalogs. Later polls reuse
    the cached sizes for an hour between checks.
  - How far each transcript was read is kept in
    `~/.cache/herdr-context-monitor/app-state.json`, so each run reads only what
    was appended.
- Not part of the mod: the repository's `herdr-app/dock.py` (standard library)
  does the docking in one step for those who want it. It copies `main.js`,
  `mod.json`, `monitor.py` and `database.svg` into `~/.config/herdr-app/mods/context-monitor/`
  and runs `herdr-app check`. Its layout: `--layout <name>`, else the one it
  used last time, else the layout on screen (`herdr-app status`) if that is a
  personal one (not an `installed.json` `layout:<name>` key) with the Agents
  list in a column, else `main-context`. In a personal layout it adds the
  block above if missing or shrinks the block from its old default height. It records
  `{layout, mode: "joined"}`. Moving to another layout takes the block out of
  the old one (and removes an untouched `main-context` nothing else shares).
  Otherwise it writes `layouts/main-context.json`: Herdr's current
  `layouts/main.json` with the block above and `cloned_from: {name: "main",
  version}`. It records what it wrote, the `main.json` it used and its own path
  in `mods/context-monitor/.dock.json` (hidden, so it doesn't reload the mod).
  Re-run, it rebuilds from the latest `main`, keeping root look keys (theme,
  fonts) changed in `main-context`, and refuses to replace a layout whose tree
  differs from what it wrote unless `--force`, which keeps
  `main-context.json.old-<time>`.
  Other mods can share `main-context`: a block showing a mod that is neither
  this one nor Herdr's own (the keys of `installed.json` not starting with
  `layout:`) is another mod's. It doesn't count as a hand edit, and a rebuild
  puts it back after its previous sibling, else before its next, else at the
  end of its parent; one whose parent is gone is reported, with the old layout
  kept. Mods only insert their own blocks, never wrap other nodes. If the app reports an error for the new
  layout, it puts the previous file back. `--status` and `--undock` (back to
  `main`; removes an untouched `main-context`).
- Programs the mod runs: `python3` (3.9+, standard library only), `hc`,
  `lsof`, `ps`. It reads files outside its folder (`~/.claude`, `~/.codex`,
  `layouts/main.json`) and writes one cache file in
  `~/.cache/herdr-context-monitor/`. It never writes a layout. No network.

## Watch out

- The folder name is in `main.js` (`NAME`): rename it there if the mod is
  installed under another name.
- `herdr-app check` writes `gpui-kit.d.ts` and `jsconfig.json` into the folder
  it checks; they aren't part of the mod.
- Only Claude Code, Codex and Antigravity agents on this computer get readings.
- A Claude session that hasn't answered yet has no transcript, so no line.
- If Claude's local catalog has no usable window for a model, the monitor falls
  back to 200k until a request passes 200k. To supply a known 1M window from
  the first response, add the override to the mod's `config.json`:
  `{ "context_windows": { "claude-opus-5-5": 1000000 } }`.
- Don't animate or redraw on a timer: the poll only calls `cx.notify()` when
  something shown changed.
- On a Mac without the developer tools, `/usr/bin/python3` is a stand-in: it
  exits 1 with `xcode-select: note: No developer tools were found, requesting
  install.` on stderr and shows the system's install prompt. A missing program
  makes `exec` reject with "cannot run …"; a timeout ("took longer than …") is
  only retried.
- `"place"` docking needs the app to see the folder at start, and the shipped
  layouts can't take new blocks: put the block in the person's own layout, or
  use the kept window, which works in every layout without a restart.
- Herdr never updates a personal layout: one cloned from `main` stays as it was
  when Herdr ships a new `main`.
- Toasts show one short line: a long command in a toast's body is cut off.
- If a Herdr update moves the Agents list out of a sidebar column, `dock.py`
  stops with an error and leaves `main-context` as it was.

## Done when

- `herdr-app check mods/context-monitor` passes.
- `herdr-app do mod.open --name context-monitor` shows the window; running it
  again closes it; reopened, it comes back where it was dropped, and after a
  restart of the app.
- With a Claude Code agent running here, its line reads the same percentage as
  that session's `/context` (rounded), and its tooltip the same tokens.
- A reading above 50% is bold and red; at 50% or below, `dim`.
- Clicking a line focuses that agent's pane.
- Docked after the `agents` block, it sits under Agents in a single 32px row.
  Each populated pill opens a list for its range and leaves the dock height fixed.
- With the panel docked by `dock.py` and shown, a `main.json` that differs from
  the recorded one brings one "Herdr updated its main layout" toast, and none
  on the next start for the same `main`.
- `python3 mods/context-monitor/monitor.py` prints
  `{"readings": {"t3": {"pct": …, "used": …, "window": …}}}` for the same agents.
