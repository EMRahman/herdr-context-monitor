// Context Monitor: how full each agent's context window is, grouped by usage.
// The readings come from monitor.py beside this file, which reads the
// transcripts of Claude Code, Codex and Antigravity on this computer.

import { View, div, image, svg } from "gpui-kit";
import { Popover, v_flex, h_flex } from "gpui-base";
import { agents, appHome, checking, closeOverlay, exec, notify, props, readTextFile, run, settings, shared, theme } from "herdr";

// This folder's name: rename it here too if the mod is installed under another.
const NAME = "context-monitor";
const POLL_MS = 5000;
// Use above this share is shown in bold red: models get less reliable as their context fills.
const HIGH = 50;
// Side padding, as the sidebar's other sections (Spaces, Agents, Prompts) use; the gap before the pills.
const PAD = 16;
const TITLE_GAP = 8;
const GROUPS = [
  { id: "low", label: "<25%", range: "Under 25% used" },
  { id: "medium", label: "≤50%", range: "25% to 50% used" },
  { id: "high", label: ">50%", range: "Over 50% used" },
];
const EMPTY = "Claude Code, Codex and Antigravity agents on this computer show here after their first answer.";

// Drops what an agent puts before its title: a status glyph or spinner and the separator
// after it, and Codex's blinking "[ ! ] " / "[ . ] " mark.
const clean = (s) => (s ?? "").replace(/^[^\p{L}\p{N}\p{Ps}\p{Pi}]+/u, "").replace(/^\[ [!.] \] /, "").trim();

// 126000 -> "126k", 1000000 -> "1M".
const count = (n) => (n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`);

const local = (a) => !a.address.includes("/");

const NO_PYTHON = "Python 3 is needed to read context use. On macOS, install it with: xcode-select --install. Click here to try again.";

// No usable python3: it isn't there ("cannot run python3"), or it is macOS's stand-in, which asks
// to install the developer tools every time it runs and exits.
const noPython = (r) => r.code !== 0 && /xcode-select|developer tools/i.test(r.stderr);

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
    this.openGroup = null;
    this.error = null;
    this.width = Infinity;
    // Measured natural widths (window pixels) of the title and of what sits right of it.
    this.titleWidth = 0;
    this.endWidth = 0;
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
    // Without Python, asking again every 5 seconds would bring macOS's install prompt back each
    // time: wait until the person clicks to try again.
    if (this.busy || this.halted) return;
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
      const dir = `${appHome()}/mods/${NAME}`;
      let r = null;
      try {
        // Keep the existing arguments so this works while an older monitor.py is being updated.
        r = await exec("python3", [`${dir}/monitor.py`, "--app", "--config-dir", dir], { timeout: 20000 });
      } catch (e) {
        const why = String(e?.message ?? e);
        // a slow run is tried again on the next poll; a python3 that can't start is not
        if (/cannot run/i.test(why)) this.halted = true;
        error = this.halted ? NO_PYTHON : why;
      }
      if (r && noPython(r)) {
        this.halted = true;
        error = NO_PYTHON;
      } else if (r && r.code === 0) {
        try {
          readings = JSON.parse(r.stdout).readings ?? {};
        } catch {
          error = "monitor.py printed something that isn't a reading";
        }
      } else if (r) {
        error = r.stderr.trim().split("\n").pop() || `monitor.py exited with ${r.code}`;
      }
      this.busy = false;
      // Every redraw redraws the whole window: only when what's shown changes.
      if (JSON.stringify(readings) === JSON.stringify(this.readings) && error === this.error) return;
      this.readings = readings;
      this.error = error;
      cx.notify();
    });
  }

  // Python may be there now: poll again.
  retry(cx) {
    this.halted = false;
    this.poll(cx);
  }

  pill(t, group, members, rail, empty) {
    const { id, label, range } = group;
    const total = members.length;
    const digits = String(total);
    const high = id === "high" && total > 0;
    const open = this.openGroup === id && total > 0;
    const color = high ? t.ansi1 : t.dim;
    const countColor = total && !high ? t.foreground : color;
    const summary = `${range} · ${total} ${total === 1 ? "session" : "sessions"}`;
    // The rail shares its width after 1px of padding on each side and two 1px gaps.
    const railCellWidth = (this.width - 4) / GROUPS.length;
    const countSize = rail ? Math.max(1, Math.min(9, railCellWidth / (digits.length * 0.65))) : 12;
    const base = (rail ? v_flex() : h_flex())
      .id(`ctx-pill-${id}`)
      .h(rail ? 26 : 22)
      .items_center()
      .justify_center()
      .rounded_full()
      .bg(open ? t.selected : t.hover)
      .accessibility_label(summary)
      .tooltip(empty ? `${summary}\n${EMPTY}` : summary)
      .child(svg(`mods/${NAME}/database.svg`).size(rail ? 9 : 11).flex_none().text_color(color));
    // The rail has room for the count only; wider, the range sits in the pill after its count.
    const trigger = rail
      ? base
        .w_full()
        .overflow_hidden()
        .text_size(countSize)
        .font_medium()
        .text_color(countColor)
        .when(high, (el) => el.font_bold())
        .child(digits)
      : base
        .flex_none()
        .pl(7)
        .pr(8)
        .gap(4)
        .child(div().text_size(countSize).font_medium().text_color(countColor).when(high, (el) => el.font_bold()).child(digits))
        .child(div().text_size(11).text_color(t.dim).child(label));
    if (!total) return trigger;
    return Popover.new(`ctx-${id}`)
      .anchor("top_right")
      .open(open)
      .on_open_change((next, cx) => {
        if (next) this.openGroup = id;
        else if (this.openGroup === id) this.openGroup = null;
        cx.notify();
      })
      .trigger(trigger
        .cursor_pointer()
        .hover((s) => s.bg(t.selected))
      )
      .when(open, (el) => el.content(
        v_flex()
          .w(280)
          .p(4)
          .text_size(13)
          .rounded(8)
          .border_1()
          .border_color(t.border)
          .bg(t.popover)
          .shadow_sm()
          .child(
            v_flex()
              .id(`ctx-list-${id}`)
              .max_h(224)
              .overflow_y_scrollbar()
              .children(members.map((a) => this.row(t, a, this.readings[a.address]))),
          ),
      ));
  }

  row(t, a, r) {
    const high = r.pct > HIGH;
    const value = div()
      .flex_none()
      .text_size(12)
      .text_color(high ? t.ansi1 : t.dim)
      .when(high, (el) => el.font_bold())
      .child(`⛁ ${r.pct}%`);
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
      .on_click((_e, cx) => {
        this.openGroup = null;
        cx.notify();
        run("pane.focus", { pane: a.pane });
      });
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
    const overlay = props()?.overlay;
    // Sizes from on_size_change are window pixels: scale the logical ones to match.
    const zoom = Number(settings()?.zoom) || 1;
    const room = this.width - 2 * PAD * zoom;
    // Too narrow for the labelled pills: the rail shows icons and counts only.
    const rail = !overlay && room < (this.endWidth || 200 * zoom);
    // The title only shows whole: where it would be cut, the pills have the row.
    const showTitle = !rail && room - TITLE_GAP * zoom - this.endWidth >= (this.titleWidth || 80 * zoom);
    // Fullest first; equal ones keep the agents' own order (sort is stable).
    const shown = agents()
      .filter((a) => local(a) && this.readings[a.address])
      .sort((a, b) => this.readings[b.address].pct - this.readings[a.address].pct);
    const buckets = [[], [], []];
    for (const a of shown) {
      const pct = this.readings[a.address].pct;
      buckets[pct < 25 ? 0 : pct <= HIGH ? 1 : 2].push(a);
    }
    if (!GROUPS.some(({ id }, i) => id === this.openGroup && buckets[i].length)) this.openGroup = null;
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
    const status = h_flex()
      .id("ctx-note-mark")
      .h(22)
      .px(8)
      .flex_none()
      .items_center()
      .justify_center()
      .rounded_full()
      .text_size(12)
      .font_bold()
      .text_color(t.ansi1)
      .hover((s) => s.bg(t.hover))
      .accessibility_label(this.error ?? "")
      .tooltip(this.error ?? "")
      .when(this.halted, (el) => el.cursor_pointer().on_click((_e, cx) => this.retry(cx)))
      .child(rail ? "!" : this.halted ? "Retry" : "Context unavailable");
    const pills = h_flex()
      .id("ctx-pills")
      .gap(rail ? 1 : 4)
      .when(rail, (el) => el.w_full())
      .when(!rail, (el) => el.flex_none())
      // Let the popover handle the press before it reaches the window's drag surface.
      .when(Boolean(overlay), (el) => el.on_mouse_down("left", (_e, cx) => cx.stop_propagation()))
      .children(GROUPS.map((group, i) => {
        const pill = this.pill(t, group, buckets[i], rail, shown.length === 0);
        return rail ? div().flex_1().min_w(0).child(pill) : pill;
      }));
    // Everything right of the title, measured at its own width so the title knows what's left.
    const end = h_flex()
      .id("ctx-end")
      .items_center()
      .gap(4)
      .when(rail, (el) => el.w_full())
      .when(!rail, (el) => el.flex_none().on_size_change((size, cx) => {
        if (size.width === this.endWidth) return;
        this.endWidth = size.width;
        cx.notify();
      }))
      .child(this.error ? status : pills)
      .when(Boolean(overlay), (el) => el.child(close));
    const title = h_flex()
      .flex_1()
      .min_w(0)
      .overflow_hidden()
      .child(
        div()
          .id("ctx-title")
          .flex_none()
          .text_size(11.5)
          .font_medium()
          .text_color(t.dim)
          .on_size_change((size, cx) => {
            if (size.width === this.titleWidth) return;
            this.titleWidth = size.width;
            cx.notify();
          })
          .child("Context Usage"),
      );
    return h_flex()
      .id("root")
      .w_full()
      .h(32)
      .px(rail ? 1 : PAD)
      .gap(TITLE_GAP)
      .items_center()
      .justify_center()
      .on_size_change((size, cx) => {
        if (size.width === this.width) return;
        this.width = size.width;
        cx.notify();
      })
      .when(!overlay, (el) => el.bg(t.chrome).border_t(1).border_color(t.border))
      .when(showTitle, (el) => el.child(title))
      .child(end);
  }
}
