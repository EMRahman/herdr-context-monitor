"""Tests for herdr-app/dock.py, the helper that installs and docks the mod. Standard library only."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MOD = Path(__file__).resolve().parent.parent / "herdr-app/mods/context-monitor"
DOCK = MOD.parent.parent / "dock.py"

MAIN = {
    "id": "root", "type": "row", "titlebar": "none",
    "children": [
        {"id": "sidebar", "type": "column", "children": [
            {"id": "brand", "type": "block", "content": {"mod": "workspace-chrome"}},
            {"id": "sidebar-lists", "type": "column", "children": [
                {"id": "spaces", "type": "block", "content": {"mod": "spaces"}},
                {"id": "agents", "type": "block", "content": {"mod": "agents"}},
            ]},
        ]},
        {"id": "main", "type": "block", "content": {"tab": "active"}},
    ],
}


class DockTest(unittest.TestCase):
    """A fresh dock.py per test, with the Herdr app's home in a temporary folder and no app running."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "layouts").mkdir()
        (self.home / "mods/context-monitor").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HERDR_APP_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("dock", DOCK)
        self.d = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.d)
        self.calls = []
        self.status = None
        self.d.app = self.fake_app
        sleep = mock.patch.object(self.d.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.set_main(MAIN)
        shipped = ["agents", "spaces", "workspace-chrome", "workspace-status", "files", "layout:main"]
        (self.home / "installed.json").write_text(json.dumps({name: {} for name in shipped}))

    def fake_app(self, *args, timeout=20):
        self.calls.append(args)
        if args[0] == "--version":
            return mock.Mock(returncode=0, stdout="herdr-app 0.1.16\n")
        if args[0] == "status":
            return mock.Mock(returncode=0, stdout=json.dumps(self.status)) if self.status else None
        if args[0] == "do" and args[1] == "layout.switch" and self.status:
            self.status["layout"] = json.loads(args[2])["name"]
        return mock.Mock(returncode=0, stdout="", stderr="")

    def set_main(self, doc):
        (self.home / "layouts/main.json").write_text(json.dumps(doc, indent=2))

    def docked(self):
        return json.loads((self.home / "layouts/main-context.json").read_text())

    def lists(self, doc):
        return doc["children"][0]["children"][1]["children"]


class BuildTests(DockTest):
    def test_the_block_goes_right_after_the_agents_list(self):
        doc = self.d.build(MAIN)
        self.assertEqual([c["id"] for c in self.lists(doc)], ["spaces", "agents", "context"])
        self.assertEqual(self.lists(doc)[2]["content"], {"mod": "context-monitor"})
        self.assertEqual(doc["cloned_from"], {"name": "main", "version": "0.1.16"})
        self.assertNotIn("cloned_from", MAIN)

    def test_a_taken_id_gets_another(self):
        main = json.loads(json.dumps(MAIN))
        main["children"][1]["id"] = "context"
        self.assertEqual(self.lists(self.d.build(main))[2]["id"], "context-monitor")

    def test_a_main_without_an_agents_column_is_refused(self):
        main = {"id": "root", "type": "row", "children": [{"id": "agents", "type": "block", "content": {"mod": "agents"}}]}
        with self.assertRaises(self.d.DockError):
            self.d.build(main)


class DockingTests(DockTest):
    def test_docking_writes_the_layout_and_shows_it(self):
        self.status = {"layout": "main", "errors": []}
        self.assertTrue(self.d.dock())
        self.assertEqual([c["id"] for c in self.lists(self.docked())], ["spaces", "agents", "context"])
        self.assertEqual(self.status["layout"], "main-context")
        self.assertFalse(self.d.dock(), "a second run changes nothing")

    def test_a_new_main_is_picked_up_by_running_again_keeping_look_changes(self):
        self.d.dock()
        layout = self.docked()
        layout["theme"] = "moss"
        (self.home / "layouts/main-context.json").write_text(json.dumps(layout))
        main = json.loads(json.dumps(MAIN))
        main["titlebar"] = "system"
        main["children"].append({"id": "status", "type": "block", "content": {"mod": "workspace-status"}})
        self.set_main(main)
        self.assertEqual(self.d.state()["stale"], True)
        self.assertTrue(self.d.dock())
        layout = self.docked()
        self.assertEqual(layout["children"][-1]["id"], "status")
        self.assertEqual((layout["theme"], layout["titlebar"]), ("moss", "system"))
        self.assertFalse(self.d.state()["stale"])

    def test_the_record_holds_what_the_mod_compares(self):
        self.d.dock()
        record = json.loads(self.d.RECORD.read_text())
        self.assertEqual(record["main_text"], (self.home / "layouts/main.json").read_text())
        self.assertEqual((record["layout"], record["helper"]), ("main-context", str(DOCK.resolve())))

    def test_a_hand_edited_layout_is_kept_unless_forced(self):
        self.d.dock()
        layout = self.docked()
        self.lists(layout).append({"id": "mine", "type": "block", "content": {"mod": "files"}})
        edited = json.dumps(layout)
        (self.home / "layouts/main-context.json").write_text(edited)
        self.set_main({**MAIN, "titlebar": "system"})
        with self.assertRaises(self.d.DockError):
            self.d.dock()
        self.assertEqual((self.home / "layouts/main-context.json").read_text(), edited)
        with mock.patch("builtins.print"):
            self.assertEqual(self.d.main([]), 1)
            self.d.dock(force=True)
        self.assertNotIn("mine", [c["id"] for c in self.lists(self.docked())])
        backups = list((self.home / "layouts").glob("main-context.json.old-*"))
        self.assertEqual([b.read_text() for b in backups], [edited])

    def test_a_layout_made_before_the_script_is_adopted_when_it_matches(self):
        (self.home / "layouts/main-context.json").write_text(json.dumps(self.d.build(MAIN)))
        self.assertEqual((self.d.state()["modified"], self.d.state()["stale"]), (False, True))
        self.d.dock()
        self.assertFalse(self.d.state()["stale"])

    def test_a_layout_the_app_rejects_is_put_back(self):
        self.d.dock()
        before = (self.home / "layouts/main-context.json").read_text()
        self.set_main({**MAIN, "titlebar": "system"})
        self.status = {"layout": "main-context", "errors": ["layouts/main-context.json: bad"]}
        with self.assertRaises(self.d.DockError):
            self.d.dock()
        self.assertEqual((self.home / "layouts/main-context.json").read_text(), before)

    def test_undock_switches_back_and_removes_the_layout(self):
        self.status = {"layout": "main", "errors": []}
        self.d.dock()
        self.d.undock()
        self.assertEqual(self.status["layout"], "main")
        self.assertFalse((self.home / "layouts/main-context.json").exists())
        self.assertFalse(self.d.RECORD.exists())


class SharedLayoutTests(DockTest):
    """Other mods' blocks in main-context: not hand edits, and kept through a rebuild."""

    USAGE = {"id": "usage-monitor", "type": "block", "size": {"px": 36}, "content": {"mod": "usage-monitor"}}

    def add(self, block, parent_id, index):
        layout = self.docked()
        self.d.find(layout, parent_id)["children"].insert(index, block)
        (self.home / "layouts/main-context.json").write_text(json.dumps(layout))

    def test_another_mods_block_is_not_a_hand_edit(self):
        self.d.dock()
        self.add(self.USAGE, "sidebar", 1)
        self.assertFalse(self.d.state()["modified"])

    def test_a_shipped_mods_block_added_by_hand_still_is(self):
        self.d.dock()
        self.add({"id": "mine", "type": "block", "content": {"mod": "files"}}, "sidebar", 1)
        self.assertTrue(self.d.state()["modified"])

    def test_a_rebuild_keeps_it_beside_the_same_neighbours(self):
        self.d.dock()
        self.add(self.USAGE, "sidebar", 1)
        main = json.loads(json.dumps(MAIN))
        main["children"][0]["children"].insert(0, {"id": "new-top", "type": "block", "content": {"mod": "spaces"}})
        self.set_main(main)
        self.assertTrue(self.d.dock())
        self.assertEqual([c["id"] for c in self.docked()["children"][0]["children"]],
                         ["new-top", "brand", "usage-monitor", "sidebar-lists"])
        self.assertFalse(self.d.state()["modified"])
        self.assertFalse(self.d.dock(), "a second run changes nothing")

    def test_without_its_previous_neighbour_it_goes_before_the_next_else_last(self):
        self.d.dock()
        self.add(self.USAGE, "sidebar", 1)
        main = json.loads(json.dumps(MAIN))
        del main["children"][0]["children"][0]  # brand, its previous neighbour, is gone
        self.set_main(main)
        self.d.dock()
        self.assertEqual([c["id"] for c in self.docked()["children"][0]["children"]], ["usage-monitor", "sidebar-lists"])
        tree = {"id": "col", "type": "column", "children": [{"id": "x", "type": "block"}]}
        lost = self.d.reinsert(tree, [{"node": self.USAGE, "parent": "col", "after": "a", "before": "b"}])
        self.assertEqual(([c["id"] for c in tree["children"]], lost), (["x", "usage-monitor"], []))

    def test_a_block_whose_parent_is_gone_is_reported_not_dropped_silently(self):
        self.d.dock()
        self.add(self.USAGE, "sidebar-lists", 0)
        before = (self.home / "layouts/main-context.json").read_text()
        main = json.loads(json.dumps(MAIN))
        lists = main["children"][0]["children"].pop(1)
        main["children"][0]["children"].extend(lists["children"])  # sidebar-lists is gone
        self.set_main(main)
        with mock.patch("builtins.print") as printed:
            self.d.dock()
        self.assertIn("usage-monitor", printed.call_args[0][0])
        backups = list((self.home / "layouts").glob("main-context.json.old-*"))
        self.assertEqual([b.read_text() for b in backups], [before])

    def test_force_drops_hand_edits_but_keeps_other_mods_blocks(self):
        self.d.dock()
        self.add(self.USAGE, "sidebar", 1)
        self.add({"id": "mine", "type": "block", "content": {"mod": "files"}}, "sidebar", 0)
        with mock.patch("builtins.print"):
            self.d.dock(force=True)
        ids = [c["id"] for c in self.docked()["children"][0]["children"]]
        self.assertEqual(ids, ["brand", "usage-monitor", "sidebar-lists"])

    def test_without_herdrs_list_of_its_mods_nothing_counts_as_another_mods(self):
        (self.home / "installed.json").unlink()
        self.d.dock()
        self.add(self.USAGE, "sidebar", 1)
        self.assertTrue(self.d.state()["modified"])


class JoinTests(DockTest):
    """Docking into a personal layout on screen (another mod's), instead of building main-context."""

    USAGE = {"id": "usage-monitor", "type": "block", "size": {"px": 36}, "content": {"mod": "usage-monitor"}}

    def setUp(self):
        super().setUp()
        theirs = json.loads(json.dumps(MAIN))
        theirs["cloned_from"] = {"name": "main"}
        theirs["children"][0]["children"].append(self.USAGE)
        self.theirs = theirs
        self.write_layout("herdr-with-usage", theirs)
        self.status = {"layout": "herdr-with-usage", "errors": []}

    def write_layout(self, name, doc):
        (self.home / "layouts" / f"{name}.json").write_text(json.dumps(doc, indent=2))

    def layout(self, name):
        path = self.home / "layouts" / f"{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def run_main(self, *args):
        with mock.patch("builtins.print"):
            return self.d.main(list(args))

    def mods(self, doc):
        return [c.get("content", {}).get("mod") for c in self.lists(doc)]

    def test_a_personal_layout_on_screen_is_joined_by_only_adding_the_block(self):
        self.assertEqual(self.run_main(), 0)
        joined = self.layout("herdr-with-usage")
        self.assertEqual(self.mods(joined), ["spaces", "agents", "context-monitor"])
        self.assertEqual(self.d.strip(joined, self.d.is_ours)[0], self.theirs)
        self.assertIsNone(self.layout("main-context"))
        record = json.loads(self.d.RECORD.read_text())
        self.assertEqual((record["layout"], record["mode"]), ("herdr-with-usage", "joined"))
        self.assertNotIn("main_text", record, "the mod's update toast is for main-context only")
        before = (self.home / "layouts/herdr-with-usage.json").read_text()
        self.run_main()
        self.assertEqual((self.home / "layouts/herdr-with-usage.json").read_text(), before)

    def test_a_shipped_layout_on_screen_gets_main_context(self):
        self.status["layout"] = "main"
        self.run_main()
        self.assertEqual(self.mods(self.layout("main-context")), ["spaces", "agents", "context-monitor"])
        self.assertNotIn("context-monitor", self.mods(self.layout("herdr-with-usage")))
        self.assertEqual(self.status["layout"], "main-context")

    def test_a_personal_layout_without_an_agents_column_gets_main_context(self):
        self.write_layout("herdr-with-usage", {"id": "root", "type": "row", "children": [self.USAGE]})
        self.run_main()
        self.assertIsNotNone(self.layout("main-context"))

    def test_the_choice_is_remembered_over_the_layout_on_screen(self):
        self.status["layout"] = "main"
        self.run_main()
        self.status["layout"] = "herdr-with-usage"
        self.run_main()
        self.assertNotIn("context-monitor", self.mods(self.layout("herdr-with-usage")))
        self.assertEqual(json.loads(self.d.RECORD.read_text())["layout"], "main-context")

    def test_a_remembered_layout_that_is_gone_is_forgotten(self):
        self.run_main()
        (self.home / "layouts/herdr-with-usage.json").unlink()
        self.status["layout"] = "main"
        self.run_main()
        self.assertEqual(json.loads(self.d.RECORD.read_text())["layout"], "main-context")

    def test_moving_takes_the_block_out_of_the_old_layout(self):
        self.run_main()
        self.run_main("--layout", "main-context")
        self.assertEqual(self.layout("herdr-with-usage"), self.theirs)
        self.assertIn("context-monitor", self.mods(self.layout("main-context")))
        # back again: an untouched main-context with nothing else in it goes
        self.run_main("--layout", "herdr-with-usage")
        self.assertIsNone(self.layout("main-context"))
        self.assertIn("context-monitor", self.mods(self.layout("herdr-with-usage")))

    def test_leaving_a_main_context_another_mod_shares_keeps_it(self):
        self.status["layout"] = "main"
        self.run_main()
        shared = self.layout("main-context")
        shared["children"][0]["children"].append(self.USAGE)
        self.write_layout("main-context", shared)
        self.run_main("--layout", "herdr-with-usage")
        kept = self.layout("main-context")
        self.assertNotIn("context-monitor", self.mods(kept))
        self.assertEqual(kept["children"][0]["children"][-1]["id"], "usage-monitor")

    def share_main_context(self):
        self.status["layout"] = "main"
        self.run_main()
        shared = self.layout("main-context")
        shared["children"][0]["children"].append(self.USAGE)
        self.write_layout("main-context", shared)

    def test_coming_back_to_a_shared_main_context_needs_no_force(self):
        self.share_main_context()
        self.run_main("--layout", "herdr-with-usage")
        self.assertEqual(self.run_main("--layout", "main-context"), 0)
        back = self.layout("main-context")
        self.assertIn("context-monitor", self.mods(back))
        self.assertEqual(back["children"][0]["children"][-1]["id"], "usage-monitor")
        self.assertNotIn("context-monitor", self.mods(self.layout("herdr-with-usage")))

    def test_coming_back_after_herdr_changed_main_rebuilds_it(self):
        self.share_main_context()
        self.run_main("--layout", "herdr-with-usage")
        self.set_main({**MAIN, "titlebar": "system"})
        self.assertEqual(self.run_main("--layout", "main-context"), 0)
        back = self.layout("main-context")
        self.assertEqual(back["titlebar"], "system")
        self.assertEqual(back["children"][0]["children"][-1]["id"], "usage-monitor")

    def test_docking_again_after_undocking_from_a_shared_main_context(self):
        self.share_main_context()
        self.run_main("--undock")
        self.assertNotIn("context-monitor", self.mods(self.layout("main-context")))
        self.assertTrue(self.d.BUILT.exists(), "kept while main-context is")
        self.assertEqual(self.run_main(), 0)
        self.assertIn("context-monitor", self.mods(self.layout("main-context")))

    def test_another_mods_block_that_took_our_old_id_is_kept(self):
        self.share_main_context()
        self.run_main("--layout", "herdr-with-usage")
        # with the panel gone, another mod takes the free id "context"
        taken = {"id": "context", "type": "block", "content": {"mod": "someone-else"}}
        layout = self.layout("main-context")
        self.d.find(layout, "sidebar-lists")["children"].append(taken)
        self.write_layout("main-context", layout)
        self.assertEqual(self.run_main("--layout", "main-context"), 0)
        back = self.layout("main-context")
        # theirs went in right after agents, so it goes back there; ours takes another id
        self.assertEqual(self.mods(back), ["spaces", "agents", "someone-else", "context-monitor"])
        self.assertEqual([c["id"] for c in self.lists(back)], ["spaces", "agents", "context", "context-monitor"])
        self.assertEqual(back["children"][0]["children"][-1]["id"], "usage-monitor")

    def test_a_different_node_with_a_kept_blocks_id_is_reported_not_skipped(self):
        tree = {"id": "col", "type": "column", "children": [{"id": "x", "type": "block", "content": {"mod": "spaces"}}]}
        theirs = {"id": "x", "type": "block", "content": {"mod": "someone-else"}}
        lost = self.d.reinsert(tree, [{"node": theirs, "parent": "col", "after": None, "before": None}])
        self.assertEqual([e["node"] for e in lost], [theirs])
        self.assertIn("id x", lost[0]["reason"])
        same = {"id": "x", "type": "block", "content": {"mod": "spaces"}}
        self.assertEqual(self.d.reinsert(tree, [{"node": same, "parent": "col", "after": None, "before": None}]), [])

    def test_an_unshared_main_context_goes_with_its_record(self):
        self.status["layout"] = "main"
        self.run_main()
        self.run_main("--undock")
        self.assertIsNone(self.layout("main-context"))
        self.assertFalse(self.d.BUILT.exists())

    def test_undock_from_a_joined_layout_takes_out_only_the_block(self):
        self.run_main()
        self.run_main("--undock")
        self.assertEqual(self.layout("herdr-with-usage"), self.theirs)
        self.assertFalse(self.d.RECORD.exists())
        self.assertEqual(self.status["layout"], "herdr-with-usage")

    def test_a_shipped_layout_cant_be_asked_for(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(self.d.main(["--layout", "main"]), 1)
        self.assertIn("read-only", str(printed.call_args[0][0]))

    def test_status_says_where_it_is(self):
        self.run_main()
        with mock.patch("builtins.print") as printed:
            self.d.main(["--status"])
        self.assertEqual(json.loads(printed.call_args[0][0]),
                         {"layout": "herdr-with-usage", "mode": "joined", "docked": True})


class InstallTests(DockTest):
    def test_installing_copies_only_the_mod_and_drops_an_old_helper(self):
        (self.home / "mods/context-monitor/dock.py").write_text("# from an older install")
        with mock.patch("builtins.print"):
            self.d.install()
        installed = sorted(p.name for p in (self.home / "mods/context-monitor").iterdir())
        self.assertEqual(installed, sorted(self.d.MOD_FILES))
        self.assertIn(("check", str(self.home / "mods/context-monitor")), [c[:2] for c in self.calls])

    def test_the_mod_folder_is_only_the_mod(self):
        shipped = sorted(p.name for p in MOD.iterdir() if p.name not in ("gpui-kit.d.ts", "herdr.d.ts", "jsconfig.json"))
        self.assertEqual(shipped, sorted(self.d.MOD_FILES))


if __name__ == "__main__":
    unittest.main()
