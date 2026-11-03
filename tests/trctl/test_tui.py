"""Tests for the curses UI: pure helpers, plus an end-to-end smoke test in tmux."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # kali-utils root
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "trctl"))

from fake_daemon import FakeDaemon  # noqa: E402
from serve_fake import demo_torrents  # noqa: E402
from trctl.tui import Col, Field, FormError, LineEdit, ListState, complete_path, layout  # noqa: E402


class TestLineEdit(unittest.TestCase):
    def test_editing_keys(self):
        e = LineEdit("hello world")
        e.handle("C-w")
        self.assertEqual(e.text, "hello ")
        e.handle("HOME")
        e.handle(">")
        self.assertEqual(e.text, ">hello ")
        e.handle("END")
        e.handle("BACKSPACE")
        e.handle("LEFT")
        e.handle("DELETE")
        self.assertEqual(e.text, ">hell")
        e.handle("C-u")
        self.assertEqual(e.text, "")
        self.assertFalse(e.handle("F5"))

    def test_render_scrolls_to_cursor(self):
        e = LineEdit("x" * 50 + "END")
        visible, cx = e.render(10)
        self.assertTrue(visible.endswith("END"))
        self.assertLessEqual(cx, 10)


class TestLayoutAndState(unittest.TestCase):
    def test_drops_columns_by_priority(self):
        cols = [Col("a", "A", 4), Col("name", "Name", None), Col("b", "B", 30, drop=2), Col("c", "C", 30, drop=1)]
        active, flex = layout(cols, 80)
        self.assertEqual([c.key for c in active], ["a", "name", "c"])
        self.assertGreaterEqual(flex, 20)

    def test_list_state(self):
        s = ListState()
        s.navigate("END", 10)
        s.clamp(50, 10)
        self.assertEqual((s.index, s.top), (49, 40))
        s.navigate("PGUP", 10)
        s.clamp(50, 10)
        self.assertEqual(s.index, 39)


class TestFields(unittest.TestCase):
    def test_int_field_blank_and_invalid(self):
        f = Field("x", "Limit", "int", None)
        self.assertIsNone(f.parsed())
        self.assertFalse(f.changed())
        f.edit.set("abc")
        with self.assertRaises(FormError):
            f.parsed()
        f.edit.set("300")
        self.assertEqual(f.parsed(), 300)
        self.assertTrue(f.changed())

    def test_float_unchanged_formatting(self):
        f = Field("r", "Ratio", "float", 2.0)
        self.assertEqual(f.edit.text, "2")
        self.assertFalse(f.changed())

    def test_choice_cycles(self):
        f = Field("p", "Prio", "choice", 0, [(-1, "low"), (0, "normal"), (1, "high")])
        f.cycle(1)
        self.assertEqual(f.value, 1)
        f.cycle(1)
        self.assertEqual(f.value, -1)

    def test_complete_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(f"{tmp}/downloads")
            os.mkdir(f"{tmp}/docs")
            new, names = complete_path(f"{tmp}/do")
            self.assertEqual(new, f"{tmp}/do")
            self.assertEqual(sorted(names), ["docs/", "downloads/"])
            new, names = complete_path(f"{tmp}/dow")
            self.assertEqual(new, f"{tmp}/downloads/")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestTUISmoke(unittest.TestCase):
    """Drive the real curses UI inside a detached tmux session."""

    def setUp(self):
        self.daemon = FakeDaemon(demo_torrents()).__enter__()
        self.session = f"trctl-test-{os.getpid()}"
        env = (f"TRCTL_CONFIG=/nonexistent TRCTL_URL={self.daemon.url} "
               f"exec {sys.executable} '{ROOT / 'bin' / 'trctl'}' watch")
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session, "-x", "130", "-y", "30",
                        "bash", "-c", env], check=True)
        self.wait_for("Big.Buck.Bunny")

    def tearDown(self):
        subprocess.run(["tmux", "kill-session", "-t", self.session], capture_output=True)
        self.daemon.__exit__(None, None, None)

    def keys(self, *keys):
        for k in keys:
            subprocess.run(["tmux", "send-keys", "-t", self.session, k], check=True)
            time.sleep(0.15)

    def screen(self) -> str:
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", self.session],
                              capture_output=True, text=True).stdout

    def wait_for(self, text, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if text in (s := self.screen()):
                return s
            time.sleep(0.1)
        self.fail(f"{text!r} never appeared on screen:\n{self.screen()}")

    def test_end_to_end(self):
        # search, open details, files tab: skip a file
        self.keys("/", "Bunny", "Enter", "Enter")
        self.wait_for("1 Info")
        self.keys("2")
        self.wait_for("7 files · 7 wanted")
        self.keys("Space")
        self.wait_for("7 files · 6 wanted")
        # per-torrent settings: set a download limit
        self.keys("c")
        self.wait_for("Download limit (KB/s)")
        self.keys("250", "Enter")
        self.wait_for("Updated settings")
        bunny = next(t for t in self.daemon.torrents if t["id"] == 2)
        self.assertEqual((bunny["downloadLimited"], bunny["downloadLimit"]), (True, 250))
        # back to the list, add a magnet
        self.keys("Escape", "Escape", "a")
        self.wait_for("Add torrent")
        self.keys("magnet:?xt=urn:btih:feedface", "Enter")
        self.wait_for("Added")
        self.assertTrue(any("feedface" in t["name"] for t in self.daemon.torrents))
        # remove the debian torrent, deleting data
        self.keys("/", "debian", "Enter", "d")
        self.wait_for("Remove torrents")
        self.keys("D")
        self.wait_for("deleted its data")
        self.assertFalse(any("debian" in t["name"] for t in self.daemon.torrents))
        self.assertTrue([a for m, a in self.daemon.calls if m == "torrent-remove"][-1]["delete-local-data"])
        # quit cleanly
        self.keys("q")
        time.sleep(0.5)
        self.assertNotIn(self.session, subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout)


if __name__ == "__main__":
    unittest.main()
