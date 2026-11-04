"""The kvctl commands, run in-process against temp folders and a fake Kavita."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tomllib
import unittest
from unittest import mock

from kvctl import cli, config
from tests.kvctl.helpers import API_KEY, FakeKavita, Workspace, make_epub


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.fake = FakeKavita([{"id": 1, "name": "Books", "type": 2, "folders": [self.ws.books]}]).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.ws.cfg.update(url=self.fake.url, api_key=API_KEY)
        env = {"KVCTL_CONFIG": f"{self.ws.root}/config.toml", "TRCTL_CONFIG": "/nonexistent", "TRCTL_URL": "http://127.0.0.1:9/transmission/rpc"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for key in ("KVCTL_API_KEY", "KVCTL_URL", "NO_COLOR"):
            os.environ.pop(key, None)
        config.save(self.ws.cfg)

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--color", "never", *argv])
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv) -> str:
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, out + err)
        return out


class TestKavitaCommands(Base):
    def test_status(self):
        out = self.ok("status")
        self.assertIn("API key works; Kavita 0.9.1.4; 1 library", out)
        self.assertIn("Imports scan library 'Books'", out)
        self.assertNotIn(API_KEY, out)

    def test_status_reports_a_bad_key(self):
        self.ws.cfg["api_key"] = "wrong"
        config.save(self.ws.cfg)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("rejected the API key", out)

    def test_status_without_kavita(self):
        self.ws.cfg["url"] = "http://127.0.0.1:9"
        config.save(self.ws.cfg)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", out)

    def test_library_lifecycle(self):
        self.assertIn("Books", self.ok("libraries"))
        self.ok("library", "create", "Novels", f"{self.ws.root}/novels")
        lib = self.fake.libraries[-1]
        self.assertEqual((lib["name"], lib["type"]), ("Novels", 2))
        self.ok("library", "rename", "Novels", "Fiction")
        self.assertEqual(lib["name"], "Fiction")
        self.ok("library", "folders", "Fiction", "--add", f"{self.ws.root}/more")
        self.assertEqual(lib["folders"], [f"{self.ws.root}/novels", f"{self.ws.root}/more"])
        self.ok("library", "folders", "Fiction", "--remove", f"{self.ws.root}/novels")
        self.assertEqual(lib["folders"], [f"{self.ws.root}/more"])
        code, _, err = self.run_cli("library", "folders", "Fiction", "--remove", f"{self.ws.root}/more")
        self.assertEqual(code, 1)
        self.assertIn("at least one folder", err)
        self.ok("library", "scan", str(lib["id"]), "--force")
        self.assertEqual(self.fake.scans()[-1]["force"], "true")
        self.ok("scan-all")
        self.ok("library", "delete", "Fiction", "-y")
        self.assertEqual(len(self.fake.libraries), 1)

    def test_delete_needs_confirmation(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("library", "delete", "Books")
        self.assertEqual(code, 1)
        self.assertIn("pass -y", err)
        self.assertEqual(len(self.fake.libraries), 1)

    def test_unknown_library(self):
        code, _, err = self.run_cli("library", "scan", "Nope")
        self.assertEqual(code, 1)
        self.assertIn("no Kavita library", err)

    def test_series_and_search(self):
        self.fake.series = [{"id": 5, "name": "Saga", "pages": 300, "libraryId": 1, "libraryName": "Books"}]
        self.assertIn("Saga", self.ok("series", "Books"))
        self.assertEqual(json.loads(self.ok("series", "--json"))[0]["id"], 5)
        self.assertIn("Saga", self.ok("search", "sag"))
        self.assertIn("No matches", self.ok("search", "zzz"))


class TestImportCommands(Base):
    def test_inbox_plan_import_undo(self):
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "A Book", ["Ring, Seth"])
        open(f"{self.ws.inbox}/Pack/junk.nfo", "w").close()
        self.assertIn("Pack", self.ok("--offline", "inbox"))
        data = json.loads(self.ok("--offline", "plan", "Pack", "--json"))
        self.assertEqual(data["books"][0]["authors"], ["Seth Ring"])
        self.assertEqual(data["delete"], ["junk.nfo"])
        self.assertEqual(self.ws.tree(self.ws.books), [])     # plan changes nothing

        out = self.ok("--offline", "plan", "Pack")
        self.assertIn("Deletes 1 other file(s): junk.nfo", out)

        out = self.ok("--offline", "import", "Pack", "-y")
        self.assertIn("Imported 1 book(s)", out)
        self.assertIn("A Book - Seth Ring.epub", " ".join(self.ws.tree(self.ws.books)))
        self.assertEqual(self.fake.scans()[-1], {"libraryId": "1", "force": "true"})    # a normal scan can drop volumes

        self.assertIn("A Book", self.ok("books", "-q", "book"))
        self.assertIn("Seth Ring", self.ok("authors"))
        self.assertIn("import", self.ok("history"))
        self.ok("undo", "1", "-y")
        self.assertEqual(self.ws.tree(self.ws.books), [])
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/Pack/a.epub"))

    def test_import_needs_confirmation(self):
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "A Book")
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("--offline", "import", "Pack")
        self.assertEqual(code, 1)
        self.assertIn("pass -y", err)
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/Pack/a.epub"))

    def test_import_by_path_says_it_keeps_other_files(self):
        other = f"{self.ws.root}/Desk"
        make_epub(f"{other}/a.epub", "A Book")
        open(f"{other}/letter.txt", "w").close()
        out = self.ok("--offline", "import", other, "-y")
        self.assertIn("Leaves 1 other file(s) in place", out)
        self.assertEqual(self.ws.tree(other), ["letter.txt"])

    def test_unknown_source(self):
        code, _, err = self.run_cli("--offline", "plan", "nothing-like-this")
        self.assertEqual(code, 1)
        self.assertIn("not a path", err)

    def test_merge_authors_shows_then_applies(self):
        self.ws.add_library_book("One", "Ring, Seth", 1)
        out = self.ok("merge-authors")
        self.assertIn("Ring, Seth -> Seth Ring", out)
        self.assertIn("Nothing changed", out)
        self.assertTrue(os.path.isdir(f"{self.ws.books}/Ring, Seth"))
        self.assertIn("not First Last", self.ok("authors"))
        self.assertIn("Renamed 1 book(s)", self.ok("merge-authors", "--apply", "-y"))
        self.assertTrue(os.path.isdir(f"{self.ws.books}/Seth Ring/One (1)"))
        self.assertIn("already", self.ok("merge-authors"))


class TestSettings(Base):
    def stored(self) -> dict:
        with open(config.config_path(), "rb") as f:
            return tomllib.load(f)

    def test_config_hides_the_key(self):
        out = self.ok("config")
        self.assertNotIn(API_KEY, out)
        self.assertIn("api_key            set", out)

    def test_config_refuses_the_key_on_the_command_line(self):
        code, _, err = self.run_cli("config", "set", "api_key", "secret")
        self.assertEqual(code, 1)
        self.assertIn("kvctl setup", err)
        self.assertEqual(self.stored()["api_key"], API_KEY)

    def test_config_set(self):
        self.ok("config", "set", "min_free_gb", "5")
        self.ok("config", "set", "online", "yes")
        self.ok("config", "set", "inbox_dirs", "/a, /b")
        s = self.stored()
        self.assertEqual((s["min_free_gb"], s["online"], s["inbox_dirs"]), (5, True, ["/a", "/b"]))
        code, _, err = self.run_cli("config", "set", "nope", "1")
        self.assertEqual(code, 1)
        code, _, err = self.run_cli("config", "set", "min_free_gb", "many")
        self.assertIn("whole number", err)

    def test_environment_overrides_are_not_saved(self):
        with mock.patch.dict(os.environ, {"KVCTL_API_KEY": "from-env", "KVCTL_URL": "http://env:1"}):
            self.ok("config", "set", "min_free_gb", "5")
        s = self.stored()
        self.assertEqual((s["api_key"], s["url"]), (API_KEY, self.fake.url))

    def setup_with(self, url: str, key: str) -> tuple[int, str, str]:
        tty = mock.Mock()
        tty.isatty.return_value = True
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: True
        with mock.patch("sys.stdin", tty), mock.patch("builtins.input", return_value=url), \
                mock.patch("kvctl.cli.getpass.getpass", return_value=key), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--color", "never", "setup"])
        return code, out.getvalue(), err.getvalue()

    def test_setup_tests_then_saves_privately_and_creates_the_watched_folder(self):
        os.remove(config.config_path())
        watched = f"{self.ws.root}/watch-me"
        with mock.patch.dict(config.DEFAULTS, {"inbox_dirs": [watched], "books_dir": self.ws.books, "database": f"{self.ws.root}/s.db"}):
            code, out, err = self.setup_with(self.fake.url, API_KEY)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(API_KEY, out + err)
        self.assertEqual(stat.S_IMODE(os.stat(config.config_path()).st_mode), 0o600)
        self.assertEqual(self.stored()["api_key"], API_KEY)
        self.assertTrue(os.path.isdir(watched))

    def test_setup_with_a_bad_key_saves_nothing(self):
        code, out, err = self.setup_with(self.fake.url, "wrong-key")
        self.assertEqual(code, 1)
        self.assertIn("rejected", err)
        self.assertEqual(self.stored()["api_key"], API_KEY)

    def test_setup_needs_a_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("setup")
        self.assertEqual(code, 1)
        self.assertIn("needs a terminal", err)


if __name__ == "__main__":
    unittest.main()
