"""Reproductions from the code review: each failed before its fix."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "trctl"))

from fake_daemon import FakeDaemon  # noqa: E402
from serve_fake import demo_torrents  # noqa: E402
from kutil.tui import ListState  # noqa: E402
from trctl import config  # noqa: E402
from trctl.cli import CLIError, parse_file_spec  # noqa: E402
from trctl.cli import main  # noqa: E402
from trctl.rpc import Client, RPCError  # noqa: E402
from trctl.tui import App  # noqa: E402


def one_shot_server(reply: bytes) -> str:
    """Accepts one connection, sends `reply` (perhaps nothing) and hangs up."""
    srv = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        if reply:
            conn.sendall(reply)
        conn.close()
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/transmission/rpc"


class TestBrokenConnections(unittest.TestCase):
    CASES = {"hangs up with no reply": b"",
             "cut off mid-body": b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"res",
             "not text": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n\xff\xfe"}

    def test_they_are_rpc_errors(self):
        for name, reply in self.CASES.items():
            with self.subTest(name), self.assertRaises(RPCError):
                Client(one_shot_server(reply), timeout=3).call("session-get")


class TestFileSpecs(unittest.TestCase):
    def test_an_empty_spec_is_refused_rather_than_meaning_every_file(self):
        for spec in ("", " ", ",", " , "):
            with self.subTest(repr(spec)), self.assertRaises(CLIError):
                parse_file_spec(spec, ["a.mkv", "b.mkv"])

    def test_real_specs_still_work(self):
        self.assertEqual(parse_file_spec("1,*b*", ["a.mkv", "b.mkv"]), [1])

    def test_an_empty_skip_sends_nothing(self):
        with FakeDaemon(demo_torrents()) as d, mock.patch.dict(os.environ, {"TRCTL_CONFIG": "/nonexistent", "TRCTL_URL": d.url, "NO_COLOR": "1"}):
            tid = str(d.torrents[0]["id"])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["files", tid, "--skip", ""])
            self.assertNotEqual(code, 0)
            self.assertFalse([a for m, a in d.calls if m == "torrent-set"])


class TestDoubleClick(unittest.TestCase):
    def test_it_opens_the_row_clicked(self):
        app = App.__new__(App)
        torrents = [{"id": 1}, {"id": 2}, {"id": 3}]
        app.view, app.list, app.selected_id, app.body_y = "list", ListState(), 1, 5
        app.tab, app.tab_state = 0, {}
        app.visible = lambda: torrents
        opened = []

        def current():      # the real one: it syncs the cursor to selected_id, which is how the wrong torrent opened
            app._sync_cursor(torrents)
            return torrents[app.list.index]
        app.current = current
        app.open_detail = lambda tab: opened.append(app.current()["id"])
        app._sync_cursor = lambda items: App._sync_cursor(app, items)
        with mock.patch("curses.getmouse", return_value=(0, 3, 6, 0, 0x8)), mock.patch("curses.BUTTON1_DOUBLE_CLICKED", 0x8), \
                mock.patch("curses.BUTTON1_CLICKED", 0x4):
            app.handle_mouse()
        self.assertEqual(opened, [2])


class TestSavedPassword(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name, "c.toml")
        path.write_text('url = "http://home:9091/transmission/rpc"\nusername = "alice"\npassword = "s3cr3t"\n')
        env = mock.patch.dict(os.environ, {"TRCTL_CONFIG": str(path)})
        env.start()
        self.addCleanup(env.stop)
        for key in ("TRCTL_URL", "TRCTL_USERNAME", "TRCTL_PASSWORD"):
            os.environ.pop(key, None)

    def settings(self, *a, **kw):
        with mock.patch("sys.stdin", io.StringIO("")):
            return config.load_settings(*a, **kw)

    def test_it_is_used_for_the_saved_server(self):
        s = self.settings()
        self.assertEqual((s.username, s.password), ("alice", "s3cr3t"))
        s = self.settings("home:9091")
        self.assertEqual((s.username, s.password), ("alice", "s3cr3t"))

    def test_it_never_goes_to_another_server(self):
        for run in (lambda: self.settings("http://other-host:9091"),
                    lambda: (os.environ.__setitem__("TRCTL_URL", "http://other-host:9091"), self.settings())[1]):
            s = run()
            self.assertEqual((s.username, s.password), (None, None))

    def test_it_is_not_used_for_another_user(self):
        self.assertIsNone(self.settings(cli_user="bob").password)
        os.environ["TRCTL_USERNAME"] = "bob"
        self.addCleanup(os.environ.pop, "TRCTL_USERNAME", None)
        s = self.settings()
        self.assertEqual((s.username, s.password), ("bob", None))

    def test_a_password_given_for_this_run_is_used(self):
        os.environ["TRCTL_PASSWORD"] = "given"
        self.addCleanup(os.environ.pop, "TRCTL_PASSWORD", None)
        self.assertEqual(self.settings("http://other-host:9091").password, "given")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestRemovedWhileOpen(unittest.TestCase):
    def test_the_details_screen_survives_and_says_so(self):
        session = f"trctl-gone-{os.getpid()}"
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True)
        with FakeDaemon(demo_torrents()) as d:
            env = f"TRCTL_CONFIG=/nonexistent TRCTL_URL={d.url} LANG=C.UTF-8 TERM=xterm-256color"
            subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "120", "-y", "30",
                            f"{env} {sys.executable} '{ROOT / 'bin' / 'trctl'}' watch; sleep 5"], check=True)
            time.sleep(1.5)
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)
            time.sleep(1)
            d.torrents.clear()
            end = time.time() + 6
            screen = ""
            while time.time() < end and "no longer exists" not in screen:
                screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", session], capture_output=True, text=True).stdout
                time.sleep(0.1)
            self.assertIn("no longer exists", screen)


if __name__ == "__main__":
    unittest.main()
