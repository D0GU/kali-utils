"""End-to-end tests: run trctl commands against the fake daemon."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # kali-utils root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_daemon import FakeDaemon, make_torrent  # noqa: E402
from trctl import fmt  # noqa: E402
from trctl.cli import CLIError, main, parse_file_spec  # noqa: E402
from trctl.config import normalize_url  # noqa: E402


def sample_torrents():
    return [
        make_torrent(1, "Ubuntu 24.04 Desktop", [("ubuntu.iso", 6_000_000_000)], done=1.0,
                     status=6, rate_up=50_000),
        make_torrent(2, "Some.Show.S01.1080p",
                     [("E01.mkv", 2_000_000_000), ("E02.mkv", 2_000_000_000),
                      ("Subs/E01.srt", 40_000), ("sample.mkv", 50_000_000), ("info.nfo", 1_000)],
                     done=0.4, status=4, rate_down=3_000_000, labels=["tv"]),
        make_torrent(3, "Debian netinst", [("debian.iso", 700_000_000)], done=0.1, status=0),
    ]


class CLITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.daemon = FakeDaemon(sample_torrents()).__enter__()
        self.env = mock.patch.dict(os.environ, {
            "TRCTL_CONFIG": os.path.join(self.tmp.name, "config.toml"),
            "TRCTL_URL": self.daemon.url, "NO_COLOR": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.daemon.__exit__(None, None, None)
        self.tmp.cleanup()

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def methods(self):
        return [m for m, _ in self.daemon.calls]


class TestList(CLITestCase):
    def test_default_command_lists_all(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        for name in ("Ubuntu 24.04 Desktop", "Some.Show.S01.1080p", "Debian netinst"):
            self.assertIn(name, out)
        self.assertIn("3 torrents", out)

    def test_status_filter(self):
        _, out, _ = self.run_cli("list", "-s", "downloading")
        self.assertIn("Some.Show", out)
        self.assertNotIn("Ubuntu", out)
        _, out, _ = self.run_cli("list", "-s", "done,paused")
        self.assertIn("Ubuntu", out)
        self.assertIn("Debian", out)
        self.assertNotIn("Some.Show", out)

    def test_name_glob_sort_json(self):
        _, out, _ = self.run_cli("list", "-n", "*iso*")  # no match on names
        self.assertIn("No torrents", out)
        _, out, _ = self.run_cli("list", "--json", "-S", "size", "-r")
        data = json.loads(out)
        self.assertEqual([t["id"] for t in data], [1, 2, 3])

    def test_ids_only_and_label(self):
        _, out, _ = self.run_cli("list", "--ids", "-l", "tv")
        self.assertEqual(out.split(), ["2"])

    def test_bad_status(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["list", "-s", "nope"])


class TestSelectors(CLITestCase):
    def test_name_hash_range(self):
        _, out, _ = self.run_cli("stop", "show")
        self.assertIn("Stopped: [2]", out)
        _, out, _ = self.run_cli("start", "0101010101")
        self.assertIn("Started: [1]", out)
        _, out, _ = self.run_cli("verify", "1-2")
        self.assertIn("[1]", out)
        self.assertIn("[2]", out)
        _, out, _ = self.run_cli("reannounce", "1,3")
        self.assertIn("[3]", out)

    def test_unknown(self):
        code, _, err = self.run_cli("stop", "99")
        self.assertEqual(code, 1)
        self.assertIn("no torrent with id 99", err)
        code, _, err = self.run_cli("stop", "nothing-like-this")
        self.assertIn("no torrent matches", err)

    def test_start_now(self):
        self.run_cli("start", "--now", "3")
        self.assertIn("torrent-start-now", self.methods())


class TestRemove(CLITestCase):
    def test_requires_confirmation_when_not_tty(self):
        code, _, err = self.run_cli("rm", "3")
        self.assertEqual(code, 1)
        self.assertIn("-y/--yes", err)
        self.assertEqual(len(self.daemon.torrents), 3)

    def test_remove_with_delete(self):
        code, out, _ = self.run_cli("rm", "-D", "-y", "debian")
        self.assertEqual(code, 0)
        self.assertIn("deleted their data", out)
        self.assertEqual([t["id"] for t in self.daemon.torrents], [1, 2])
        self.assertTrue(self.daemon.calls[-1][1]["delete-local-data"])


class TestAdd(CLITestCase):
    def test_add_magnet_file_hash(self):
        torrent_file = Path(self.tmp.name, "x.torrent")
        torrent_file.write_bytes(b"d4:infod4:name1:xee")
        code, out, _ = self.run_cli("add", "-p", "-d", "/data/new", "-l", "linux",
                                    "magnet:?xt=urn:btih:abc", str(torrent_file), "a" * 40)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("Added"), 3)
        adds = [a for m, a in self.daemon.calls if m == "torrent-add"]
        self.assertTrue(all(a["paused"] and a["download-dir"] == "/data/new" for a in adds))
        self.assertIn("metainfo", adds[1])
        self.assertTrue(adds[2]["filename"].startswith("magnet:?xt=urn:btih:aaaa"))

    def test_add_duplicate_and_bad_source(self):
        self.run_cli("add", "magnet:?xt=urn:btih:abc")
        code, out, err = self.run_cli("add", "magnet:?xt=urn:btih:abc", "/no/such/file")
        self.assertEqual(code, 1)
        self.assertIn("Already present", out)
        self.assertIn("not a file", err)


class TestFiles(CLITestCase):
    def test_list_files(self):
        code, out, _ = self.run_cli("files", "2")
        self.assertEqual(code, 0)
        self.assertIn("E01.mkv", out)
        self.assertIn("Subs/E01.srt", out)
        self.assertIn("5 files · 5 wanted", out)

    def test_only_and_priority(self):
        code, out, _ = self.run_cli("files", "2", "--only", "*.mkv", "--skip", "sample", "--high", "0")
        self.assertEqual(code, 1)  # sample.mkv is both wanted (via --only) and skipped
        code, out, _ = self.run_cli("files", "2", "--only", "E0*.mkv", "--high", "0", "--low", "1")
        self.assertEqual(code, 0, out)
        t = self.daemon.torrents[1]
        self.assertEqual([s["wanted"] for s in t["fileStats"]], [True, True, False, False, False])
        self.assertEqual([s["priority"] for s in t["fileStats"]][:2], [1, -1])
        self.assertIn("2 wanted", out)

    def test_files_requires_single(self):
        code, _, err = self.run_cli("files", "all")
        self.assertEqual(code, 1)
        self.assertIn("exactly one torrent", err)

    def test_json(self):
        _, out, _ = self.run_cli("files", "2", "--json")
        data = json.loads(out)
        self.assertEqual(data[2]["path"], "Some.Show.S01.1080p/Subs/E01.srt")


class TestMoveRename(CLITestCase):
    def test_move(self):
        code, out, _ = self.run_cli("mv", "2", "/data/tv")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.torrents[1]["downloadDir"], "/data/tv")
        self.assertTrue(self.daemon.calls[-1][1]["move"])
        code, _, err = self.run_cli("mv", "2", "relative/dir")
        self.assertIn("absolute", err)

    def test_move_multiple_needs_confirmation(self):
        code, _, err = self.run_cli("mv", "all", "/x")
        self.assertEqual(code, 1)
        code, _, _ = self.run_cli("mv", "-y", "--no-move", "all", "/x")
        self.assertEqual(code, 0)
        self.assertFalse(self.daemon.calls[-1][1]["move"])

    def test_rename_top_level_and_inner(self):
        code, out, _ = self.run_cli("rename", "2", "Some Show S01")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.daemon.torrents[1]["name"], "Some Show S01")
        code, out, err = self.run_cli("rename", "2", "--path", "Subs", "subtitles")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.daemon.calls[-1][1]["path"], "Some Show S01/Subs")
        code, out, err = self.run_cli("rename", "2", "--path", "0", "Episode 1.mkv")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.daemon.calls[-1][1]["path"], "Some Show S01/E01.mkv")
        code, _, err = self.run_cli("rename", "2", "--path", "nope", "x")
        self.assertIn("not a file or folder", err)

    def test_rename_single_file_torrent(self):
        code, _, err = self.run_cli("rename", "3", "--path", "debian.iso", "d.iso")
        self.assertEqual(code, 1)  # single-file torrent: the path is the torrent name
        code, _, err = self.run_cli("rename", "3", "--path", "Debian netinst", "debian-12.iso")
        self.assertEqual(code, 0, err)


class TestInfoStatsWatchWait(CLITestCase):
    def test_info(self):
        code, out, _ = self.run_cli("info", "-m", "show")
        self.assertEqual(code, 0)
        self.assertIn("Some.Show.S01.1080p", out)
        self.assertIn("5 (5 wanted)", out)
        self.assertIn("magnet:?", out)

    def test_stats(self):
        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("4.1.1 (fake)", out)
        self.assertIn("Free space", out)
        self.assertIn("5000 KB/s", out)

    def test_watch_once(self):
        code, out, _ = self.run_cli("watch", "-n", "1", "-i", "0")
        self.assertEqual(code, 0)
        self.assertIn("Some.Show", out)
        self.assertIn("Debian", out)
        self.assertNotIn("Ubuntu", out)  # complete, so hidden without --all
        self.assertIn("█", out)

    def test_wait_complete_and_timeout(self):
        code, out, _ = self.run_cli("wait", "1")
        self.assertEqual(code, 0)
        self.assertIn("complete", out)
        code, _, err = self.run_cli("wait", "-i", "0.05", "-t", "0.1", "3")
        self.assertEqual(code, 124)
        self.assertIn("stopped", err)

    def test_wait_local_error(self):
        self.daemon.torrents[1]["error"] = 3
        self.daemon.torrents[1]["errorString"] = "No space left on device"
        code, _, err = self.run_cli("wait", "2")
        self.assertEqual(code, 1)
        self.assertIn("No space left", err)


class TestConnection(unittest.TestCase):
    def test_auth_and_setup(self):
        with tempfile.TemporaryDirectory() as tmp, \
                FakeDaemon(sample_torrents(), username="kali", password="s3cr3t") as d, \
                mock.patch.dict(os.environ, {"TRCTL_CONFIG": f"{tmp}/c/config.toml", "NO_COLOR": "1"}):
            os.environ.pop("TRCTL_URL", None)
            os.environ.pop("TRCTL_PASSWORD", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stdin", io.StringIO("")):
                self.assertEqual(main(["--url", d.url, "--user", "kali", "list"]), 2)
            self.assertIn("authentication failed", err.getvalue())

            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("s3cr3t\n")):
                code = main(["--url", d.url, "--user", "kali", "setup", "--non-interactive",
                             "--password-stdin"])
            self.assertEqual(code, 0, out.getvalue())
            cfg = Path(tmp, "c", "config.toml")
            self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["list"]), 0)
            self.assertIn("Ubuntu", out.getvalue())

    def test_unreachable(self):
        with mock.patch.dict(os.environ, {"TRCTL_URL": "http://127.0.0.1:1/transmission/rpc",
                                          "TRCTL_CONFIG": "/nonexistent/c.toml"}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["list"]), 1)
            self.assertIn("cannot reach", err.getvalue())


class TestHelpers(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(normalize_url("192.168.1.128"), "http://192.168.1.128:9091/transmission/rpc")
        self.assertEqual(normalize_url("nas:9000"), "http://nas:9000/transmission/rpc")
        self.assertEqual(normalize_url("https://x.example/custom/rpc"), "https://x.example/custom/rpc")

    def test_file_spec(self):
        paths = ["a.mkv", "b.mkv", "Subs/a.srt", "info.nfo"]
        self.assertEqual(parse_file_spec("0,2-3", paths), [0, 2, 3])
        self.assertEqual(parse_file_spec("*.mkv", paths), [0, 1])
        self.assertEqual(parse_file_spec("subs", paths), [2])
        self.assertEqual(parse_file_spec("all", paths), [0, 1, 2, 3])
        with self.assertRaises(CLIError):
            parse_file_spec("9", paths)

    def test_formatting(self):
        self.assertEqual(fmt.human_size(1536), "1.5 KiB")
        self.assertEqual(fmt.human_size(500), "500 B")
        self.assertEqual(fmt.human_eta(3725), "1h02m")
        self.assertEqual(fmt.human_eta(-1), "-")
        self.assertEqual(fmt.bar(0.5, 10), "█████·····")
        self.assertEqual(fmt.truncate("abcdefgh", 5), "abcd…")


if __name__ == "__main__":
    unittest.main()
