"""`bookread` as a command: what it prints, its exit codes, and that it stands alone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from bookread.library import book_key
from bookread.store import Store
from tests.bookread.helpers import Workspace, make_book, write_file

ROOT = Path(__file__).resolve().parents[2]
TEXT = [("a.xhtml", "<p>" + "word " * 2380 + "</p>")]


class CLI(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.long = make_book(f"{self.ws.books}/Long Road - Ann Lee.epub", TEXT, title="Long Road", authors=("Ann Lee",))
        self.short = make_book(f"{self.ws.books}/Short Walk.epub", [("a.xhtml", "<p>few words</p>")], title="Short Walk", authors=("Bob Ray",))

    def run_cli(self, *args):
        env = {**os.environ, "BOOKREAD_CONFIG": self.ws.config}
        return subprocess.run([sys.executable, str(ROOT / "bin" / "bookread"), *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)

    def test_version(self):
        res = self.run_cli("--version")
        self.assertEqual(res.returncode, 0)
        self.assertTrue(res.stdout.startswith("bookread "))

    def test_list_prints_books_for_a_script(self):
        res = self.run_cli("--list")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Long Road", res.stdout)
        self.assertIn("Short Walk", res.stdout)
        self.assertNotIn("\x1b", res.stdout)

    def test_list_can_be_narrowed(self):
        res = self.run_cli("--list", "short")
        self.assertIn("Short Walk", res.stdout)
        self.assertNotIn("Long Road", res.stdout)

    def test_json_includes_progress(self):
        store = Store(self.ws.database)
        store.reading_save(book_key(self.long), 1190, 2380, 0.5)
        store.close()
        books = {b["title"]: b for b in json.loads(self.run_cli("--list", "--json").stdout)}
        self.assertEqual(books["Long Road"]["percent"], 0.5)
        self.assertEqual(books["Long Road"]["seconds_left"], 300)     # 1190 words at 238 a minute
        self.assertEqual(books["Long Road"]["path"], self.long)
        self.assertEqual(books["Long Road"]["readable"], True)
        self.assertIsNone(books["Short Walk"]["percent"])

    def test_a_finished_book_has_no_time_left(self):
        store = Store(self.ws.database)
        store.reading_save(book_key(self.long), 2300, 2380, 1.0)
        store.close()
        books = {b["title"]: b for b in json.loads(self.run_cli("--list", "--json").stdout)}
        self.assertEqual(books["Long Road"]["seconds_left"], 0)

    def test_a_file_name_that_is_not_valid_text_can_be_listed(self):
        raw = os.fsencode(self.ws.books) + b"/Caf\xe9 Book.pdf"
        open(raw, "wb").close()
        for args in (("--list",), ("--list", "--json")):
            with self.subTest(args):
                res = self.run_cli(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn("Caf", res.stdout)

    def test_a_relative_database_path_works(self):
        with open(self.ws.config, "w") as f:
            f.write(f'books_dir = "{self.ws.books}"\ndatabase = "reading.db"\n')
        env = {**os.environ, "BOOKREAD_CONFIG": self.ws.config}
        res = subprocess.run([sys.executable, str(ROOT / "bin" / "bookread"), "--list"], env=env, cwd=self.ws.root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_database_that_cannot_be_opened_is_a_plain_error(self):
        with open(self.ws.config, "w") as f:
            f.write(f'books_dir = "{self.ws.books}"\ndatabase = "{self.long}/x/reading.db"\n')
        res = self.run_cli("--list")
        self.assertEqual(res.returncode, 1)
        self.assertIn("bookread: error", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_dir_overrides_the_configured_folder(self):
        make_book(f"{self.ws.root}/other/Far.epub", TEXT, title="Far")
        res = self.run_cli("--dir", f"{self.ws.root}/other", "--list")
        self.assertIn("Far", res.stdout)
        self.assertNotIn("Long Road", res.stdout)

    def test_reading_needs_a_terminal(self):
        for args in ((), ("long road",), (self.long,)):
            with self.subTest(args):
                res = self.run_cli(*args)
                self.assertEqual(res.returncode, 1)
                self.assertIn("needs a terminal", res.stderr)

    def test_no_match_is_an_error(self):
        res = self.run_cli("nothing like this")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no book matches", res.stderr)

    def test_a_file_in_another_format_is_an_error(self):
        path = write_file(f"{self.ws.root}/b.mobi")
        res = self.run_cli(path)
        self.assertEqual(res.returncode, 1)
        self.assertIn("EPUB", res.stderr)

    def test_it_works_without_any_other_tool_installed(self):
        code = ("import sys; from bookread import cli, reader; "
                "bad = [m for m in sys.modules if m.split('.')[0] in ('kvctl', 'trctl', 'plexsort', 'syswatch')]; sys.exit(repr(bad) if bad else 0)")
        res = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
