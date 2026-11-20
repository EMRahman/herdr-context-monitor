// Context Monitor: how full each agent's context window is, one line per agent.
// The readings come from monitor.py beside this file (`--app`), which reads the
// transcripts of Claude Code and Codex on this computer.

import { View, div, image, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, appHome, checking, closeOverlay, exec, notify, props, readTextFile, run, shared, theme } from "herdr";

// This folder's name: rename it here too if the mod is installed under another.
const NAME = "context-monitor";
const POLL_MS = 5000;
// Use above this share is shown in bold red: models get less reliable as their context fills.
const HIGH = 50;

// Drops what an agent puts before its title: a status glyph or spinner and the separator
// after it, and Codex's blinking "[ ! ] " / "[ . ] " mark.
const clean = (s) => (s ?? "").replace(/^[^\p{L}\p{N}\p{Ps}\p{Pi}]+/u, "").replace(/^\[ [!.] \] /, "").trim();

// 126000 -> "126k", 1000000 -> "1M".
const count = (n) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`);

const local = (a) => !a.address.includes("/");

// A short fingerprint of a text, to say "already told you about this one".
const fingerprint = (s) => {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return `${s.length}-${h.toString(16)}`;
};

// The agent's icon from the app's program table: a one-color glyph or a picture.
function icon(p, size, color) {
  if (!p?.icon) return null;
  return p.tint ? svg(p.icon).size(size).flex_none().text_color(color) : image(p.icon).w(size).h(size).flex_none();
}

export default class ContextMonitor extends View {
  init(_props, cx) {
    // terminal address -> { pct, used, window }
    this.readings = {};
    this.error = null;
    this.width = Infinity;
    this.busy = false;
    if (checking()) return;
    cx.timer.every(POLL_MS, (cx) => this.poll(cx));
    this.poll(cx);
    if (!props()?.overlay) this.checkDock(cx);
  }

  // Docked with the repository's herdr-app/dock.py, this panel's layout is that script's copy of
  // Herdr's main, and Herdr never updates a copy. When Herdr ships a new main, say how to bring the
  // copy along, once per new main. The mod only reads: placing it is the person's (or dock.py's).
  checkDock(cx) {
    cx.spawn(async () => {
      let record;
      let main;
      try {
        record = JSON.parse((await readTextFile(`${appHome()}/mods/${NAME}/.dock.json`)) ?? "null");
        main = await readTextFile(`${appHome()}/layouts/main.json`);
      } catch {
        return;
      }
      if (!record?.main_text || main == null || main === record.main_text) return;
      if (shared("layout") !== record.layout) return;
      const seen = fingerprint(main);
      if (localStorage.getItem("dock-told") === seen) return;
      localStorage.setItem("dock-told", seen);
      // Toasts show one short line: the full command is in the README.
      notify({ title: "Herdr updated its main layout", body: `Re-run dock.py to update ${record.layout}`, level: "info" });
    });
  }

  poll(cx) {
    if (this.busy) return;
    // monitor.py reads this computer's transcripts: agents on other machines get no reading.
    if (!agents().some(local)) {
      if (Object.keys(this.readings).length) {
        this.readings = {};
        cx.notify();
      }
      return;
    }
    this.busy = true;
    cx.spawn(async (cx) => {
      let readings = {};
      let error = null;
      try {
        const dir = `${appHome()}/mods/${NAME}`;
        const r = await exec("python3", [`${dir}/monitor.py`, "--app", "--config-dir", dir], { timeout: 20000 });
        if (r.code === 0) readings = JSON.parse(r.stdout).readings ?? {};
        else error = r.stderr.trim().split("\n").pop() || `monitor.py exited with ${r.code}`;
      } catch (e) {
        error = `Can't run python3: ${e?.message ?? e}`;
      }
      this.busy = false;
      // Every redraw redraws the whole window: only when what's shown changes.
      if (JSON.stringify(readings) === JSON.stringify(this.readings) && error === this.error) return;
      this.readings = readings;
      this.error = error;
      cx.notify();
    });
  }

  row(t, a, r, narrow) {
    const high = r.pct > HIGH;
    const value = div()
      .flex_none()
      .text_size(12)
      .text_color(high ? t.ansi1 : t.dim)
      .when(high, (el) => el.font_bold())
      .child(narrow ? `${r.pct}%` : `⛁ ${r.pct}%`);
    const el = h_flex()
      .id(`ctx-${a.address}`)
      .h(28)
      .px(8)
      .flex_none()
      .rounded(8)
      .items_center()
      .gap(9)
      .when(a.focused, (el) => el.bg(t.selected))
      .cursor_pointer()
      .hover((s) => s.bg(t.hover))
      .tooltip(`Context ${100 - r.pct}% left · ${count(r.used)} of ${count(r.window)} tokens`)
      .on_click(() => run("pane.focus", { pane: a.pane }));
    if (narrow) return el.justify_center().child(value);
    const pic = icon(a.program, 16, t.foreground);
    const detail = clean(a.title);
    return el
      .when(Boolean(pic), (el) => el.child(pic))
      .child(
        h_flex()
          .flex_1()
          .min_w(0)
          .gap(5)
          .items_center()
          .child(div().flex_none().max_w(110).truncate().text_color(t.foreground).child(a.space_name || a.program?.name || a.address))
          .when(Boolean(detail), (el) => el.child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(`· ${detail}`))),
      )
      .child(value);
  }

  render() {
    const t = theme();
    // A floating window (mod.open) fits its rows; a layout block fills its space.
    const overlay = props()?.overlay;
    const narrow = !overlay && this.width < 160;
    // Fullest first; equal ones keep the agents' own order (sort is stable).
    const shown = agents()
      .filter((a) => local(a) && this.readings[a.address])
      .sort((a, b) => this.readings[b.address].pct - this.readings[a.address].pct);
    const note = this.error ?? "Claude Code and Codex agents on this computer show here after their first answer.";
    const rows = shown.map((a) => this.row(t, a, this.readings[a.address], narrow));
    const close = div()
      .id("ctx-close")
      .flex_none()
      .px(4)
      .rounded(4)
      .text_color(t.dim)
      .cursor_pointer()
      .hover((s) => s.bg(t.hover).text_color(t.foreground))
      .tooltip("Close")
      // A press the mod doesn't stop drags the window.
      .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
      .on_click(() => closeOverlay(overlay))
      .child("\u00d7");
    return v_flex()
      .id("root")
      .on_size_change((size, cx) => {
        if (size.width === this.width) return;
        this.width = size.width;
        cx.notify();
      })
      .when(!overlay, (el) => el.size_full().min_h(0).bg(t.chrome).border_t(1).border_color(t.border))
      .when(Boolean(overlay), (el) => el.w_full().pb(6))
      .text_size(13)
      .when(!narrow, (el) =>
        el.child(
          h_flex()
            .px(16)
            .h(overlay ? 34 : 41)
            .pt(overlay ? 8 : 14)
            .pb(5)
            .flex_none()
            .items_center()
            .justify_between()
            .child(div().text_size(11.5).font_medium().text_color(t.dim).child("Context"))
            .when(Boolean(overlay), (el) => el.child(close)),
        ),
      )
      .when(!shown.length && !narrow, (el) =>
        el.child(div().px(16).pb(overlay ? 4 : 0).text_size(12).text_color(this.error ? t.ansi1 : t.dim).child(note)),
      )
      .when(Boolean(overlay), (el) => el.child(v_flex().px(8).children(rows)))
      .when(!overlay, (el) =>
        el.child(v_flex().id("ctx-list").flex_1().min_h(0).px(narrow ? 0 : 8).overflow_y_scrollbar().children(rows)),
      );
  }
}
