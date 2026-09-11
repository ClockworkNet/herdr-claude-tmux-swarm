"""Unit tests for the pure parts of swarm_watcher. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))

import swarm_watcher as sw  # noqa: E402

with open(os.path.join(HERE, "fixtures", "w19.json")) as fh:
    FX = json.load(fh)
PPID = {int(k): v for k, v in FX["ppid"].items()}


class SocketParsing(unittest.TestCase):
    def test_parses_pid_from_socket_name(self):
        self.assertEqual(sw.parse_socket_name("claude-swarm-66650"), 66650)

    def test_rejects_other_sockets(self):
        for name in ("default", "claude-swarm-", "claude-swarm-abc", "claude-swarm-1-2", "xclaude-swarm-5"):
            self.assertIsNone(sw.parse_socket_name(name), name)

    def test_lists_only_swarm_sockets(self):
        names = ["default", "claude-swarm-44057", "claude-swarm-66650", "junk"]
        self.assertEqual(sw.swarm_sockets_from_names(names), {"claude-swarm-44057": 44057, "claude-swarm-66650": 66650})

    def test_tmux_dir_honours_env_then_uid(self):
        self.assertEqual(sw.tmux_dir({"TMUX_TMPDIR": "/x"}, uid=502), "/x/tmux-502")
        self.assertEqual(sw.tmux_dir({}, uid=502), "/tmp/tmux-502")


class Liveness(unittest.TestCase):
    def test_alive_needs_socket_server_and_pid(self):
        self.assertTrue(sw.swarm_alive("claude-swarm-1", 1, socket_exists=lambda n: True,
                                       server_up=lambda n: True, pid_alive=lambda p: True))

    def test_dead_when_any_leg_fails(self):
        legs = dict(socket_exists=lambda n: True, server_up=lambda n: True, pid_alive=lambda p: True)
        for leg in legs:
            broken = dict(legs)
            broken[leg] = lambda *_: False
            self.assertFalse(sw.swarm_alive("claude-swarm-1", 1, **broken), leg)

    def test_pid_reuse_with_dead_server_is_dead(self):
        # claude-swarm-91622 on Sep 5: socket present, server gone, pid alive (reused). Must be dead.
        self.assertFalse(sw.swarm_alive("claude-swarm-91622", 91622, socket_exists=lambda n: True,
                                        server_up=lambda n: False, pid_alive=lambda p: True))


class PidToPane(unittest.TestCase):
    def test_lead_pid_resolves_to_its_pane_via_foreground(self):
        self.assertEqual(sw.match_pid_to_pane(66650, FX["process_info"], PPID), "w19:p2")

    def test_child_of_claude_resolves_via_shell_ancestor(self):
        # a grandchild of claude (e.g. a Bash tool process) not in foreground list
        ppid = dict(PPID)
        ppid[99999] = 66650
        self.assertEqual(sw.match_pid_to_pane(99999, FX["process_info"], ppid), "w19:p2")

    def test_attach_pane_is_not_the_owner(self):
        # 73529 is the tmux client in a hand-made attach pane; it must map to p3, never p2
        self.assertEqual(sw.match_pid_to_pane(73529, FX["process_info"], PPID), "w19:p3")

    def test_unknown_pid_maps_nowhere(self):
        self.assertIsNone(sw.match_pid_to_pane(424242, FX["process_info"], PPID))

    def test_ancestors_stop_at_init(self):
        self.assertEqual(sw.ancestors(66650, PPID), {66650, 64705, 34760})


class ViewPaneRecognition(unittest.TestCase):
    def test_recognises_tmux_attach_for_socket(self):
        self.assertTrue(sw.is_swarm_view(FX["process_info"]["w19:p3"], "claude-swarm-66650"))

    def test_rejects_other_socket_or_process(self):
        self.assertFalse(sw.is_swarm_view(FX["process_info"]["w19:p3"], "claude-swarm-1"))
        self.assertFalse(sw.is_swarm_view(FX["process_info"]["w19:p2"], "claude-swarm-66650"))

    def test_recognises_the_utf8_forcing_attach(self):
        """The pane script passes -u before -L. close_view refuses to close a pane it does not
        recognise, so a flag added ahead of -L must not make our own view look like someone else's."""
        info = {"foreground_processes": [{"name": "tmux", "argv": [
            "/opt/homebrew/bin/tmux", "-u", "-L", "claude-swarm-66650", "attach-session",
            "-t", "claude-swarm"]}]}
        self.assertTrue(sw.is_swarm_view(info, "claude-swarm-66650"))
        self.assertFalse(sw.is_swarm_view(info, "claude-swarm-1"))


class ExistingViewDiscovery(unittest.TestCase):
    """The adoption guard: what is on screen outranks what state remembers."""

    def setUp(self):
        self._ids, self._info = sw.pane_ids, sw.process_info

    def tearDown(self):
        sw.pane_ids, sw.process_info = self._ids, self._info

    def test_finds_the_pane_attached_to_that_socket(self):
        sw.process_info = lambda p: FX["process_info"].get(p, {})
        self.assertEqual(sw.find_existing_view("claude-swarm-66650", ["w19:p2", "w19:p3"]), "w19:p3")

    def test_none_when_no_pane_is_attached(self):
        sw.process_info = lambda p: FX["process_info"].get(p, {})
        self.assertIsNone(sw.find_existing_view("claude-swarm-1", ["w19:p2", "w19:p3"]))

    def test_unreadable_pane_does_not_abort_the_search(self):
        def info(p):
            if p == "w19:p2":
                raise sw.HerdrError("boom", "gone")
            return FX["process_info"].get(p, {})
        sw.process_info = info
        self.assertEqual(sw.find_existing_view("claude-swarm-66650", ["w19:p2", "w19:p3"]), "w19:p3")

    def test_defaults_to_asking_herdr_for_the_pane_list(self):
        sw.pane_ids = lambda: ["w19:p3"]
        sw.process_info = lambda p: FX["process_info"].get(p, {})
        self.assertEqual(sw.find_existing_view("claude-swarm-66650"), "w19:p3")


class StateTransaction(unittest.TestCase):
    """The duplicate-team bug: the watcher and a pane action are separate processes, each
    holding its own copy of swarms.json, and each save() writes the whole file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.watcher = sw.Runtime(self.tmp)   # long-lived loop
        self.action = sw.Runtime(self.tmp)    # short-lived `toggle`
        self.watcher.state["swarms"]["claude-swarm-1"] = {"owner_pane": "w19:p1", "view_pane": None}
        self.watcher.save()
        self.action.state = sw.load_state(self.action.state_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_txn_rereads_so_a_concurrent_open_is_not_clobbered(self):
        # the action opens a view and records it
        self.action.state["swarms"]["claude-swarm-1"]["view_pane"] = "w15:p0"
        self.action.save()
        # the watcher's next cycle runs from a copy that predates that write
        self.assertIsNone(self.watcher.state["swarms"]["claude-swarm-1"]["view_pane"])
        with self.watcher.state_txn() as st:
            self.assertEqual(st["swarms"]["claude-swarm-1"]["view_pane"], "w15:p0",
                             "cycle must see the view the action just opened")
            st["swarms"]["claude-swarm-1"]["dismissed"] = False
        on_disk = sw.load_state(self.watcher.state_path)
        self.assertEqual(on_disk["swarms"]["claude-swarm-1"]["view_pane"], "w15:p0")

    def test_txn_writes_nothing_when_the_cycle_found_no_news(self):
        before = os.stat(self.watcher.state_path).st_mtime_ns
        with self.watcher.state_txn():
            pass
        self.assertEqual(os.stat(self.watcher.state_path).st_mtime_ns, before)

    def test_txn_persists_a_mutation_the_caller_forgot_to_save(self):
        with self.watcher.state_txn() as st:
            st["swarms"]["claude-swarm-1"]["owner_pane"] = "w19:p9"
        self.assertEqual(sw.load_state(self.watcher.state_path)["swarms"]["claude-swarm-1"]["owner_pane"],
                         "w19:p9")


class Toggle(unittest.TestCase):
    """toggle_for_pane only decides which way to go; show/hide are covered by their own paths."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rt = sw.Runtime(self.tmp)
        self.calls = []
        # toggle decides, then calls the locked variants; it holds the state lock itself.
        self.rt.show_locked = lambda p: self.calls.append(("show", p)) or "shown"
        self.rt.hide_locked = lambda p: self.calls.append(("hide", p)) or "hidden"
        self._real_pane_exists = sw.pane_exists

    def track(self, **rec):
        """Seed a swarm record on disk: toggle re-reads state inside the lock, so an
        in-memory record would be discarded before the decision is made."""
        self.rt.state["swarms"]["claude-swarm-1"] = rec
        self.rt.save()

    def tearDown(self):
        sw.pane_exists = self._real_pane_exists
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shows_when_nothing_is_tracked(self):
        sw.pane_exists = lambda p: True
        self.assertEqual(self.rt.toggle_for_pane("w19:p1"), "shown")
        self.assertEqual(self.calls, [("show", "w19:p1")])

    def test_hides_from_the_owner_pane(self):
        sw.pane_exists = lambda p: True
        self.track(owner_pane="w19:p1", view_pane="w19:p3")
        self.assertEqual(self.rt.toggle_for_pane("w19:p1"), "hidden")

    def test_hides_from_the_view_pane_too(self):
        sw.pane_exists = lambda p: True
        self.track(owner_pane="w19:p1", view_pane="w19:p3")
        self.assertEqual(self.rt.toggle_for_pane("w19:p3"), "hidden")

    def test_reopens_when_the_recorded_view_pane_is_gone(self):
        """A record left behind by a pane that went away would otherwise eat a keypress."""
        sw.pane_exists = lambda p: False
        self.track(owner_pane="w19:p1", view_pane="w19:p3")
        self.assertEqual(self.rt.toggle_for_pane("w19:p1"), "shown")

    def test_ignores_a_swarm_owned_by_another_pane(self):
        sw.pane_exists = lambda p: True
        self.track(owner_pane="w19:p9", view_pane="w19:pA")
        self.assertEqual(self.rt.toggle_for_pane("w19:p1"), "shown")


class Direction(unittest.TestCase):
    def test_lead_pane_108x64_splits_down(self):
        rect = sw.rect_for_pane(FX["layout"], "w19:p2")
        self.assertEqual(rect["width"], 108)
        self.assertEqual(sw.choose_direction(rect), "down")

    def test_wide_pane_splits_right(self):
        self.assertEqual(sw.choose_direction({"width": 215, "height": 64}), "right")

    def test_missing_pane_falls_back_to_right(self):
        self.assertIsNone(sw.rect_for_pane(FX["layout"], "w19:p9"))
        self.assertEqual(sw.choose_direction(None), "right")


class State(unittest.TestCase):
    def test_roundtrip_atomic(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "swarms.json")
            st = sw.load_state(path)
            self.assertEqual(st["swarms"], {})
            st["swarms"]["claude-swarm-1"] = {"pid": 1, "owner_pane": "w1:p1", "view_pane": "w1:p2"}
            sw.save_state(path, st)
            self.assertEqual(sw.load_state(path)["swarms"]["claude-swarm-1"]["view_pane"], "w1:p2")
            self.assertEqual(sorted(os.listdir(d)), ["swarms.json"])  # no temp file left behind

    def test_corrupt_state_is_treated_as_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "swarms.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(sw.load_state(path)["swarms"], {})


if __name__ == "__main__":
    unittest.main()
