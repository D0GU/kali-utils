"""Drive the real kvctl curses UI inside a detached tmux session."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from kvctl import config, epub
from tests.kvctl.helpers import API_KEY, FakeKavita, Workspace, make_epub, write_file
from tests.trctl.fake_daemon import FakeDaemon, make_torrent

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestUI(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        ws = self.ws
        make_epub(f"{ws.downloads}/Torrent Pack/Alpha - Seth Ring.epub", "Alpha", ["Ring, Seth"])
        make_epub(f"{ws.downloads}/Torrent Pack/Beta - Seth Ring.epub", "Beta", ["Ring, Seth"])
        write_file(f"{ws.downloads}/Torrent Pack/site.url")
        make_epub(f"{ws.inbox}/Watched Book/w.epub", "Watched", ["Ann Lee"])
        ws.add_library_book("Backlist", "Ring, Seth", 56)
        self.kavita = FakeKavita([{"id": 1, "name": "Books", "type": 2, "folders": [ws.books]}],
                                 [{"id": 5, "name": "Saga Series", "pages": 300, "libraryId": 1}]).__enter__()
        self.addCleanup(self.kavita.__exit__)
        torrent = make_torrent(7, "Torrent Pack", [("Alpha - Seth Ring.epub", 10), ("Beta - Seth Ring.epub", 10), ("site.url", 1)],
                               download_dir=ws.downloads, status=6)
        self.daemon = FakeDaemon([torrent]).__enter__()
        self.addCleanup(self.daemon.__exit__, None, None, None)
        ws.cfg.update(url=self.kavita.url, api_key=API_KEY)
        env = {"KVCTL_CONFIG": f"{ws.root}/config.toml", "TRCTL_CONFIG": "/nonexistent", "TRCTL_URL": self.daemon.url}
        self.session = f"kvctl-test-{os.getpid()}"
        os.environ["KVCTL_CONFIG"] = env["KVCTL_CONFIG"]
        self.addCleanup(os.environ.pop, "KVCTL_CONFIG", None)
        config.save(ws.cfg)
        command = " ".join(f"{k}='{v}'" for k, v in env.items()) + f" exec {sys.executable} '{ROOT / 'bin' / 'kvctl'}'"
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session, "-x", "140", "-y", "32", "bash", "-c", command], check=True)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", self.session], capture_output=True)

    def keys(self, *keys):
        for k in keys:
            subprocess.run(["tmux", "send-keys", "-t", self.session, k], check=True)
            time.sleep(0.2)

    def screen(self) -> str:
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", self.session], capture_output=True, text=True).stdout

    def wait_for(self, text, timeout=6.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            if text in (s := self.screen()):
                return s
            time.sleep(0.1)
        self.fail(f"{text!r} never appeared:\n{self.screen()}")

    def test_import_books_kavita_and_history(self):
        ws = self.ws
        self.wait_for("Torrent Pack")
        self.wait_for("Watched Book")
        self.wait_for("Inbox (2)")

        # Review the torrent: nothing moves until `a`
        rows = [line for line in self.screen().splitlines() if "torrent" in line or "folder" in line]
        if "Torrent Pack" not in rows[0]:
            self.keys("j")
        self.keys("Enter")
        self.wait_for("2 of 2 book(s) to import")
        self.wait_for("Seth Ring/Alpha (57)")
        self.assertEqual(len(os.listdir(f"{ws.downloads}/Torrent Pack")), 3)

        # Edit the first book's title
        self.keys("e")
        self.wait_for("Edit: Alpha")
        self.keys("C-u", "Alpha Prime", "Enter")
        self.wait_for("Seth Ring/Alpha Prime (57)")

        # Skip the second; the first keeps its id
        self.keys("j", "Space")
        self.wait_for("1 of 2 book(s) to import")
        self.keys("k")

        # Apply: the confirmation names what it deletes
        self.keys("a")
        screen = self.wait_for("Import 1 book(s) into")
        self.assertIn("Delete 1 skipped book file(s)", screen)
        self.assertIn("Delete 1 other file(s): site.url", screen)
        self.assertIn("Remove the torrent from Transmission", screen)
        self.keys("a")
        self.wait_for("Imported 1 book(s)")
        self.assertTrue(os.path.exists(f"{ws.books}/Seth Ring/Alpha Prime (57)/Alpha Prime - Seth Ring.epub"))
        self.assertFalse(os.path.exists(f"{ws.downloads}/Torrent Pack"))
        self.assertEqual(self.daemon.torrents, [])
        self.assertEqual(self.kavita.scans()[-1]["libraryId"], "1")
        self.wait_for("Inbox (1)")

        # Books tab: search, details, merge authors
        self.keys("2")
        self.wait_for("Backlist")
        self.keys("/", "backl", "Enter")
        self.wait_for("1 book(s)")
        self.keys("Enter")
        self.wait_for("Book - e edit")
        self.keys("Escape")
        self.keys("Escape")
        self.wait_for("Alpha Prime")
        self.keys("M")
        screen = self.wait_for("Merge authors: 1 book(s)")
        self.assertIn("Ring, Seth -> Seth Ring", screen)
        self.assertTrue(os.path.isdir(f"{ws.books}/Ring, Seth"))    # shown, not done
        self.keys("Escape")
        self.assertTrue(os.path.isdir(f"{ws.books}/Ring, Seth"))    # cancel changes nothing
        self.keys("M")
        self.wait_for("Merge authors: 1 book(s)")
        self.keys("a")
        self.wait_for("Renamed 1 book(s)")
        self.assertTrue(os.path.isdir(f"{ws.books}/Seth Ring/Backlist (56)"))
        self.assertFalse(os.path.exists(f"{ws.books}/Ring, Seth"))

        # Kavita tab: scan, series, new library, delete
        self.keys("3")
        self.wait_for("Last scan")
        before = len(self.kavita.scans())
        self.keys("s")
        self.wait_for("Kavita is scanning Books")
        self.assertEqual(len(self.kavita.scans()), before + 1)
        self.keys("F")
        self.wait_for("(forced)")
        self.assertEqual(self.kavita.scans()[-1]["force"], "true")
        self.keys("Enter")
        self.wait_for("Saga Series")
        self.keys("Escape", "n")
        self.wait_for("New Kavita library")
        self.keys("Enter")
        self.wait_for("Created Books")
        self.assertEqual(len(self.kavita.libraries), 2)
        self.keys("j", "x")
        self.wait_for("Delete Books from Kavita?")
        self.keys("n")
        self.assertEqual(len(self.kavita.libraries), 2)
        self.keys("x")
        self.wait_for("Delete Books from Kavita?")
        self.keys("y")
        self.wait_for("files are untouched")
        self.assertEqual(len(self.kavita.libraries), 1)

        # History: undo the merge, then the import
        self.keys("4")
        self.wait_for("merge authors")
        self.keys("Enter")
        self.wait_for("edited")
        self.keys("Escape", "u")
        self.wait_for("Undo operation 2")
        self.keys("u")
        self.wait_for("Undid")
        self.assertTrue(os.path.isdir(f"{ws.books}/Ring, Seth/Backlist (56)"))
        self.keys("j", "u")
        screen = self.wait_for("Undo operation 1")
        self.assertIn("can't be restored", screen)
        self.keys("r")
        self.wait_for("Undid")
        self.assertTrue(os.path.exists(f"{ws.downloads}/Torrent Pack/Alpha - Seth Ring.epub"))
        self.assertTrue(any("magnet" in str(a) for m, a in self.daemon.calls if m == "torrent-add"))
        self.assertFalse(os.path.exists(f"{ws.books}/Seth Ring/Alpha Prime (57)"))

        # Help, settings, quit
        self.keys("?")
        self.wait_for("REVIEW")
        self.keys("Escape", "S")
        self.wait_for("Library to scan")
        self.keys("Escape", "q")
        time.sleep(0.5)
        self.assertNotIn(self.session, subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout)

    def test_group_series(self):
        ws = self.ws
        one = ws.add_library_book("Saga, Vol. 1", "Ann Lee", 60)
        two = ws.add_library_book("Saga, Vol. 2", "Ann Lee", 61)
        self.wait_for("Inbox (")
        self.keys("r", "2")
        self.wait_for("Saga, Vol. 2")
        self.keys("s")
        self.wait_for("Group series: 2 book(s)")
        self.keys("Escape")
        self.assertEqual(epub.read_series(f"{one}/Saga, Vol. 1 - Ann Lee.epub"), ("", None))   # cancel changes nothing
        self.keys("s")
        self.wait_for("Group series: 2 book(s)")
        self.keys("a")
        self.wait_for("Grouped 2 book(s)")
        self.assertEqual(epub.read_series(f"{two}/Saga, Vol. 2 - Ann Lee.epub"), ("Saga", 2.0))

    def test_import_shows_and_brings_its_series_partner(self):
        ws = self.ws
        older = ws.add_library_book("Deep Dive 102", "Bruce Sentar", 60)
        make_epub(f"{ws.inbox}/New Volume/a.epub", "Deep Dive 101", ["Bruce Sentar"])
        self.wait_for("Inbox (")
        self.keys("r")
        self.wait_for("New Volume")
        lines = [l for l in self.screen().splitlines() if any(x in l for x in ("New Volume", "Torrent Pack", "Watched Book"))]
        row = next(i for i, l in enumerate(lines) if "New Volume" in l)
        self.keys(*["j"] * row, "Enter")
        self.wait_for("Also adds Deep Dive 102 to Deep Dive, as #102")
        self.assertEqual(epub.read_series(f"{older}/Deep Dive 102 - Bruce Sentar.epub"), ("", None))   # shown, not done
        self.keys("a")
        self.wait_for("Add Deep Dive 102 to Deep Dive, as #102")
        self.keys("a")
        self.wait_for("Imported 1 book(s)")
        self.assertEqual(epub.read_series(f"{older}/Deep Dive 102 - Bruce Sentar.epub"), ("Deep Dive", 102.0))

    def test_mixed_torrent_says_what_stays(self):
        ws = self.ws
        make_epub(f"{ws.downloads}/Mixed/book.epub", "Mixed Book", ["Ann Lee"])
        write_file(f"{ws.downloads}/Mixed/film.mkv")
        self.daemon.torrents.append(make_torrent(8, "Mixed", [("book.epub", 10), ("film.mkv", 10)], download_dir=ws.downloads, status=6))
        self.wait_for("Inbox (")
        self.keys("r")
        self.wait_for("Mixed")
        lines = [l for l in self.screen().splitlines() if any(x in l for x in ("Mixed", "Torrent Pack", "Watched Book"))]
        row = next(i for i, l in enumerate(lines) if "Mixed" in l)
        self.keys(*["j"] * row, "Enter")
        self.wait_for("1 of 1 book(s) to import")
        self.keys("a")
        screen = self.wait_for("Import 1 book(s) into")
        self.assertIn("Keep the torrent and 1 video/music file(s)", screen)
        self.assertNotIn("Delete", screen)


if __name__ == "__main__":
    unittest.main()
