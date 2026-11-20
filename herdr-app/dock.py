#!/usr/bin/env python3
"""Installs the Context Monitor mod and docks it under the Agents list. Not part of the mod.

The gallery mod is only mods/context-monitor/: where it shows is up to the person (RECIPE.md says
how to dock it in their own layout). This helper does that in one step for anyone who wants it.

Which layout: --layout <name>, else the one chosen last time, else the layout on screen if it is
a personal one (not one Herdr ships) with an Agents list in a column, else `main-context`.
- In a personal layout someone else made, it only adds its block after the Agents list (if it
  isn't there yet). That layout stays its owner's: never rebuilt, never fingerprinted.
- Herdr's shipped layouts are read-only, so otherwise the panel goes in `main-context`, which this
  helper builds: Herdr's current `layouts/main.json` with one block added after the Agents list.
Moving to another layout takes the block out of the old one.

Herdr never updates a personal layout. When it ships a new `main`, the mod shows a toast; run this
again and `main-context` is rebuilt from the new `main`, keeping look settings changed in it
(theme, fonts). A layout edited by hand is never replaced unless --force (which keeps a copy).

Other mods can share `main-context`. The rule they and this helper follow: a block showing a mod
that is neither this one nor Herdr's own (installed.json) belongs to another mod. Those blocks
don't count as hand edits, and a rebuild puts each back beside the same neighbours. Mods only
insert their own blocks into existing containers, never wrap other nodes.

    python3 herdr-app/dock.py                  install or update the mod, dock it, show the layout
    python3 herdr-app/dock.py --layout <name>  the same, in that layout (main-context: build it)
    python3 herdr-app/dock.py --force          the same, replacing a hand-edited main-context
    python3 herdr-app/dock.py --status         print what's docked, as JSON
    python3 herdr-app/dock.py --undock         take the panel out (main-context goes if it's empty)
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MOD = "context-monitor"
SOURCE = Path(__file__).resolve().parent / "mods" / MOD
APP_HOME = Path(os.environ.get("HERDR_APP_HOME") or Path.home() / ".config/herdr-app")
LAYOUT = "main-context"
# Hidden, so writing it doesn't reload the mod; the mod reads it to tell when main has changed.
RECORD = APP_HOME / "mods" / MOD / ".dock.json"
# What the layout's root says about its shape; every other root key is look (theme, fonts, ...).
STRUCTURE = ("id", "type", "children", "cloned_from")
# The mod: what installing copies (not herdr-app check's type files, not the record).
MOD_FILES = ("main.js", "mod.json", "monitor.py")


class DockError(Exception):
    pass


def herdr_app():
    return shutil.which("herdr-app") or str(Path.home() / ".local/bin/herdr-app")


def app(*args, timeout=20):
    """Runs herdr-app; None when it isn't installed or doesn't answer."""
    try:
        return subprocess.run([herdr_app(), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def app_status():
    res = app("status", timeout=5)
    try:
        return json.loads(res.stdout) if res and res.returncode == 0 else None
    except ValueError:
        return None


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def layout_path(name):
    return APP_HOME / "layouts" / f"{name}.json"


def load_record():
    """Where the panel was docked last time, if anywhere."""
    try:
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) and isinstance(record.get("layout"), str) else None
    except (OSError, ValueError):
        return None


def built_record():
    """The record of main-context as this helper last wrote it."""
    record = load_record()
    ok = record and record["layout"] == LAYOUT and isinstance(record.get("written"), dict)
    return record if ok else None


def save_record(record):
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def ids(node):
    yield node.get("id")
    for child in node.get("children") or []:
        yield from ids(child)


def installed():
    try:
        return list(json.loads((APP_HOME / "installed.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def shipped_mods():
    """The mods Herdr ships: installed.json's keys, less its layouts ("layout:<name>")."""
    names = installed()
    return None if names is None else {name for name in names if not name.startswith("layout:")}


def shipped_layouts():
    names = installed()
    return None if names is None else {name[7:] for name in names if name.startswith("layout:")}


def foreign_to_us():
    """Whether a node is another mod's block. Without Herdr's list of its own mods, none is."""
    shipped = shipped_mods()

    def foreign(node):
        mod = (node.get("content") or {}).get("mod")
        return (shipped is not None and node.get("type") == "block" and isinstance(mod, str)
                and mod != MOD and mod not in shipped)
    return foreign


def strip(tree, match):
    """A copy of the layout without the matching blocks, and each block with where it was."""
    removed = []

    def walk(node):
        if not node.get("children"):
            return node
        kept = []
        children = node["children"]
        for i, child in enumerate(children):
            if match(child):
                removed.append({
                    "node": child,
                    "parent": node.get("id"),
                    "after": children[i - 1].get("id") if i > 0 else None,
                    "before": children[i + 1].get("id") if i + 1 < len(children) else None,
                })
            else:
                kept.append(walk(child))
        return {**node, "children": kept}

    return walk(copy.deepcopy(tree)), removed


def find(node, node_id):
    if node.get("id") == node_id:
        return node
    for child in node.get("children") or []:
        found = find(child, node_id)
        if found:
            return found
    return None


def reinsert(tree, removed):
    """Puts removed blocks back beside the same neighbours; returns the ones with nowhere to go."""
    lost = []
    for entry in removed:
        if entry["node"].get("id") and find(tree, entry["node"]["id"]):
            continue
        parent = find(tree, entry["parent"]) if entry["parent"] else None
        if not parent or not isinstance(parent.get("children"), list):
            lost.append(entry)
            continue
        siblings = parent["children"]
        ids_here = [child.get("id") for child in siblings]
        if entry["after"] in ids_here:
            index = ids_here.index(entry["after"]) + 1
        elif entry["before"] in ids_here:
            index = ids_here.index(entry["before"])
        else:
            index = len(siblings)
        siblings.insert(index, entry["node"])
    return lost


def find_agents(node, parent=None):
    """The Agents list's block and the node holding it."""
    if node.get("type") == "block" and (node.get("content") or {}).get("mod") == "agents":
        return node, parent
    for child in node.get("children") or []:
        found = find_agents(child, node)
        if found:
            return found
    return None


def agents_column(doc):
    """The Agents list's block and its column, or None when it isn't in one."""
    found = find_agents(doc)
    return found if found and found[1] and found[1].get("type") == "column" else None


def is_ours(node):
    return node.get("type") == "block" and (node.get("content") or {}).get("mod") == MOD


def has_ours(node):
    return is_ours(node) or any(has_ours(child) for child in node.get("children") or [])


def add_block(doc, where):
    """Inserts the Context block right after the Agents list, into its column (never wrapping)."""
    found = agents_column(doc)
    if not found:
        raise DockError(f"{where} has no Agents list in a sidebar column to dock under. "
                        f"Open the floating window instead: herdr-app do mod.open --name {MOD}")
    agents, column = found
    taken = set(ids(doc))
    block_id = next(i for i in ("context", "context-monitor", "context-monitor-2") if i not in taken)
    column["children"].insert(column["children"].index(agents) + 1, {
        "id": block_id, "type": "block", "size": {"px": 132}, "min": 41, "content": {"mod": MOD},
    })


def build(main):
    """Herdr's main layout with the Context block after its Agents list."""
    doc = copy.deepcopy(main)
    add_block(doc, "Herdr's main layout")
    res = app("--version", timeout=5)
    version = res.stdout.strip().split()[-1] if res and res.returncode == 0 and res.stdout.strip() else None
    doc["cloned_from"] = {"name": "main", "version": version} if version else {"name": "main"}
    return doc


def shape(doc):
    return {k: doc.get(k) for k in ("id", "type", "children")}


def looks(doc):
    return {k: v for k, v in doc.items() if k not in STRUCTURE}


def state(record=None):
    """What's docked now: whether the layout is ours, untouched, and built from the current main.
    `record`: judge main-context against this record instead of the current one."""
    main_text = read(layout_path("main"))
    if main_text is None:
        raise DockError(f"No Herdr main layout at {layout_path('main')}: is the Herdr app installed?")
    text = read(layout_path(LAYOUT))
    record = record if record is not None else built_record()
    current = None
    if text is not None:
        try:
            current = json.loads(text)
        except ValueError:
            current = None
    # other mods' blocks are theirs to add: they aren't hand edits
    foreign = foreign_to_us()
    mine = lambda doc: shape(strip(doc, foreign)[0])
    if current is None:
        modified = text is not None
    elif record:
        # look changes (theme, fonts) are carried over; anything else means it was edited
        modified = mine(current) != mine(record["written"])
    else:
        # docked before this helper kept a record: ours only if it's exactly what we'd build
        modified = mine(current) != mine(build(json.loads(main_text)))
    return {
        "layout": LAYOUT,
        "docked": text is not None,
        "modified": modified,
        "stale": text is not None and (not record or record.get("main") != digest(main_text)),
        "main_text": main_text,
        "current": current,
        "record": record,
    }


def write_layout(doc, previous_text, name=LAYOUT):
    path = layout_path(name)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    # a running app applies it within a second and keeps the last good version if it can't:
    # put the old file back then
    time.sleep(1)
    errors = [e for e in (app_status() or {}).get("errors", []) if name in json.dumps(e)]
    if errors:
        if previous_text is None:
            path.unlink()
        else:
            path.write_text(previous_text, encoding="utf-8")
        raise DockError(f"Herdr rejected the layout {name}: {errors[0]}")


def dock(force=False, show=True):
    """Writes main-context from Herdr's current main; returns whether the file changed."""
    s = state()
    previous_text = read(layout_path(LAYOUT))
    if s["modified"]:
        if not force:
            raise DockError(f"layouts/{LAYOUT}.json was changed by hand, so it wasn't replaced. "
                            "Run dock.py --force to rebuild it (a copy of yours is kept).")
        backup = layout_path(LAYOUT).with_name(f"{LAYOUT}.json.old-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(layout_path(LAYOUT), backup)
        print(f"Kept your previous layout as {backup}")
    doc = build(json.loads(s["main_text"]))
    # other mods' blocks go back beside the same neighbours in the new main
    lost = []
    if s["current"] is not None:
        lost = reinsert(doc, strip(s["current"], foreign_to_us())[1])
    if lost:
        backup = layout_path(LAYOUT).with_name(f"{LAYOUT}.json.old-{time.strftime('%Y%m%d-%H%M%S')}")
        if not backup.exists():
            shutil.copyfile(layout_path(LAYOUT), backup)
        for entry in lost:
            print(f"Couldn't put back the {entry['node'].get('content', {}).get('mod')} block "
                  f"({entry['node'].get('id')}): Herdr's new main has no {entry['parent']}. "
                  f"The previous layout is kept as {backup}.", file=sys.stderr)
    if s["current"] is not None and not s["modified"]:
        # look settings changed in the docked layout win over main's
        before = looks(s["record"]["written"]) if s["record"] else looks(doc)
        for key in set(before) | set(looks(s["current"])):
            if s["current"].get(key) != before.get(key):
                if key in s["current"]:
                    doc[key] = s["current"][key]
                else:
                    doc.pop(key, None)
    changed = json.dumps(doc, indent=2) + "\n" != previous_text
    if changed:
        write_layout(doc, previous_text)
    save_record({
        "layout": LAYOUT,
        "main": digest(s["main_text"]),
        # the mod compares this with layouts/main.json to tell when Herdr shipped a new main
        "main_text": s["main_text"],
        "helper": str(Path(__file__).resolve()),
        "written": doc,
    })
    if show:
        show_layout(LAYOUT)
    return changed


def show_layout(name):
    status = app_status()
    if status and status.get("layout") != name:
        app("do", "layout.switch", json.dumps({"name": name}))


def on_screen():
    return (app_status() or {}).get("layout")


def joinable(name):
    """A personal layout someone else made, with an Agents list in a column to dock under."""
    if not name or name == LAYOUT:
        return False
    shipped = shipped_layouts()
    if shipped is None or name in shipped:
        return False
    try:
        return agents_column(json.loads(read(layout_path(name)) or "null") or {}) is not None
    except ValueError:
        return False


def choose(explicit):
    """The layout to dock in: as asked, else as last time, else the one on screen, else main-context."""
    if explicit:
        if explicit != LAYOUT and not joinable(explicit):
            raise DockError(f"Can't dock in {explicit}: it must be a personal layout (Herdr's own are "
                            "read-only) whose Agents list is in a column. Use --layout main-context.")
        return explicit
    record = load_record()
    # the layout chosen last time, while it can still take the panel
    if record and (record["layout"] == LAYOUT or joinable(record["layout"])):
        return record["layout"]
    shown = on_screen()
    return shown if joinable(shown) else LAYOUT


def join(name, show=True):
    """Adds the block to another's personal layout if it isn't there; that layout stays theirs."""
    text = read(layout_path(name))
    doc = json.loads(text)
    changed = not has_ours(doc)
    if changed:
        add_block(doc, f"layouts/{name}.json")
        write_layout(doc, text, name)
    save_record({"layout": name, "mode": "joined", "helper": str(Path(__file__).resolve())})
    if show:
        show_layout(name)
    return changed


def remove_ours(name):
    """Takes the Context block out of a layout; True if it was there."""
    text = read(layout_path(name))
    if text is None:
        return False
    doc = json.loads(text)
    kept, removed = strip(doc, is_ours)
    if not removed:
        return False
    write_layout(kept, text, name)
    return True


def leave(record):
    """Takes the panel out of where it was. An untouched main-context with nothing else in it goes."""
    name = record["layout"]
    if name != LAYOUT or record.get("mode") == "joined":
        remove_ours(name)
        return
    s = state(record if isinstance(record.get("written"), dict) else None)
    if not s["docked"]:
        return
    others = s["current"] is not None and strip(s["current"], foreign_to_us())[1]
    if s["modified"] or others:
        # someone else's work is in it: only our block comes out
        remove_ours(LAYOUT)
        return
    if on_screen() == LAYOUT:
        app("do", "layout.switch", json.dumps({"name": "main"}))
    layout_path(LAYOUT).unlink()


def undock():
    record = load_record() or ({"layout": LAYOUT} if layout_path(LAYOUT).exists() else None)
    if record:
        leave(record)
    try:
        RECORD.unlink()
    except FileNotFoundError:
        pass
    return record and record["layout"]


def install():
    """Copies the mod from this checkout into the Herdr app and checks it."""
    target = APP_HOME / "mods" / MOD
    target.mkdir(parents=True, exist_ok=True)
    for name in MOD_FILES:
        shutil.copy2(SOURCE / name, target / name)
    # an older install kept this helper inside the mod
    try:
        (target / "dock.py").unlink()
    except FileNotFoundError:
        pass
    res = app("check", str(target), timeout=60)
    if res is not None and res.returncode != 0:
        raise DockError(f"herdr-app check failed: {(res.stderr or res.stdout).strip()}")
    print(f"Installed the mod in {target}")


def main(args):
    try:
        if "--status" in args:
            record = load_record()
            if record and record.get("mode") == "joined":
                text = read(layout_path(record["layout"]))
                print(json.dumps({"layout": record["layout"], "mode": "joined",
                                  "docked": bool(text) and has_ours(json.loads(text))}))
            else:
                s = state()
                print(json.dumps({"mode": "built", **{k: s[k] for k in ("layout", "docked", "modified", "stale")}}))
        elif "--undock" in args:
            name = undock()
            print(f"Took the Context panel out of {name}." if name else "The Context panel wasn't docked.")
        else:
            explicit = args[args.index("--layout") + 1] if "--layout" in args[:-1] else None
            target = choose(explicit)
            before = load_record()
            install()
            if target == LAYOUT:
                changed = dock(force="--force" in args)
            else:
                changed = join(target)
            if before and before["layout"] != target:
                leave(before)
                print(f"Took the Context panel out of {before['layout']}.")
            print(f"Docked under Agents in the {target} layout" + ("" if changed else " (already up to date)")
                  + ". ctrl-shift-l switches layouts.")
    except DockError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
