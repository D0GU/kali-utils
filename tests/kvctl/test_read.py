"""kvctl hands books to bookread: `kvctl read`, and `r` on the Books tab."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from bookread.library import book_key
from bookread.store import Store
from kvctl import config
from tests.bookread.helpers import make_book
from tests.kvctl.helpers import Workspace, write_file

ROOT = Path(__file__).resolve().parents[2]


def text(name: str) -> str:
    return "".join(f"<p>{name}para{i} " + "lorem ipsum dolor sit amet " * 8 + "</p>" for i in range(40))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestRead(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.session = f"kvctl-read-test-{os.getpid()}"
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", self.session], capture_output=True)
        self.reading_db = f"{self.ws.root}/reading.db"
        conf = f"{self.ws.root}/bookread.toml"
        with open(conf, "w") as f:
            f.write(f'database = "{self.reading_db}"\nbooks_dir = "{self.ws.root}/unrelated"\n')
        self.env = {"KVCTL_CONFIG": f"{self.ws.root}/config.toml", "BOOKREAD_CONFIG": conf, "TRCTL_CONFIG": "/nonexistent",
                    "TRCTL_URL": "http://127.0.0.1:1", "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        os.environ["KVCTL_CONFIG"] = self.env["KVCTL_CONFIG"]
        self.addCleanup(os.environ.pop, "KVCTL_CONFIG", None)
        config.save(self.ws.cfg)
        self.long = self.add("Long Road", "Ann Lee", 10)
        self.short = self.add("Short Walk", "Bob Ray", 11)

    def add(self, title, author, book_id):
        folder = self.ws.add_library_book(title, author, book_id)
        return make_book(glob.glob(glob.escape(folder) + "/*.epub")[0], [("a.xhtml", text(title.split()[0]))], title=title, authors=(author,))

    def start(self, *args):
        env = " ".join(f"{k}='{v}'" for k, v in self.env.items())
        quoted = " ".join("'" + a + "'" for a in args)
        command = f"{env} exec {sys.executable} '{ROOT / 'bin' / 'kvctl'}' {quoted}"
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session, "-x", "100", "-y", "30", "bash", "-c", command], check=True)

    def keys(self, *keys):
        for k in keys:
            subprocess.run(["tmux", "send-keys", "-t", self.session, k], check=True)
            time.sleep(0.1)

    def screen(self, colours=False):
        return subprocess.run(["tmux", "capture-pane", "-p", *(["-e"] if colours else []), "-t", self.session], capture_output=True, text=True).stdout

    def wait_for(self, wanted, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if wanted in (s := self.screen()):
                return s
            time.sleep(0.05)
        self.fail(f"{wanted!r} never appeared:\n{self.screen()}")

    def test_r_on_the_books_tab_reads_and_q_comes_back(self):
        self.start()
        self.wait_for("Books (2)")     # keys pressed while it's still loading are dropped
        self.keys("2")
        self.wait_for("Long Road")
        self.keys("r")
        self.wait_for("Longpara0")
        self.keys("t")
        self.wait_for("Theme: Paper")
        self.keys("Space", "q")
        screen = self.wait_for("Books (2)")
        self.assertIn("Long Road", screen)
        self.assertNotIn("48;5;255", self.screen(colours=True))    # the reader's colours end with the reader
        store = Store(self.reading_db)
        self.addCleanup(store.close)
        self.assertGreater(store.reading_get(book_key(self.long))["position"], 0)

    def test_a_format_the_reader_cannot_open_is_explained(self):
        folder = self.ws.add_library_book("Aaa Mobi", "Cal Poe", 12)
        for f in glob.glob(glob.escape(folder) + "/*.epub"):
            os.remove(f)
        write_file(f"{folder}/Aaa Mobi - Cal Poe.mobi", b"x")
        self.start()
        self.wait_for("Books (3)")
        self.keys("2")
        self.wait_for("Aaa Mobi")
        rows = [l for l in self.screen().splitlines() if any(t in l for t in ("Aaa Mobi", "Long Road", "Short Walk"))]
        at = next(i for i, l in enumerate(rows) if "Aaa Mobi" in l)
        self.keys(*["j"] * at, "r")
        self.wait_for("The reader opens EPUB")

    def test_kvctl_read_with_a_book_opens_it(self):
        self.start("read", "11")
        self.wait_for("Shortpara0")

    def test_kvctl_read_with_several_matches_shows_the_menu_of_its_own_library(self):
        self.add("Long Pier", "Dee Ash", 13)
        self.start("read", "long")
        self.wait_for("Long Pier")
        screen = self.wait_for("Long Road")
        self.assertNotIn("Short Walk", screen)

    def test_kvctl_read_alone_shows_the_menu_of_its_own_library(self):
        self.start("read")
        self.wait_for("Short Walk")


class TestReadCommandLine(unittest.TestCase):
    def test_no_match_is_an_error(self):
        ws = Workspace()
        self.addCleanup(ws.close)
        env = {**os.environ, "KVCTL_CONFIG": f"{ws.root}/config.toml", "TRCTL_CONFIG": "/nonexistent"}
        os.environ["KVCTL_CONFIG"] = env["KVCTL_CONFIG"]
        self.addCleanup(os.environ.pop, "KVCTL_CONFIG", None)
        config.save(ws.cfg)
        res = subprocess.run([sys.executable, str(ROOT / "bin" / "kvctl"), "read", "nothing"], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("no book matches", res.stderr)


if __name__ == "__main__":
    unittest.main()
