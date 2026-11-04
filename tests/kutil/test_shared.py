"""Code the tools share: cleaning text from untrusted names, and starting up under an unavailable language setting."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kutil import fmt
from kutil.text import clean_text

ROOT = Path(__file__).resolve().parents[2]
BAD_LOCALE = "xx_YY.UTF-8"


class TestCleanText(unittest.TestCase):
    def test_table(self):
        table = {"plain": "plain", "a\x1b[31mb": "a[31mb", "title\x07": "title", "sof­t": "soft", "café": "café",
                 "keeps\ttabs\nand lines": "keeps\ttabs\nand lines", "日本語": "日本語"}
        for raw, want in table.items():
            with self.subTest(raw):
                self.assertEqual(clean_text(raw), want)


class TestTables(unittest.TestCase):
    def test_a_torrent_or_file_name_cannot_send_escapes(self):
        out = fmt.table(["Name"], [["evil\x1b]0;PWNED\x07name"], [("styled\x1b[2J", "bold")]], "<")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)


class TestLocale(unittest.TestCase):
    def test_an_unavailable_language_setting_is_not_an_error(self):
        env = {**os.environ, "LC_ALL": BAD_LOCALE}
        res = subprocess.run([sys.executable, "-c", "from kutil.tui import set_locale; set_locale(); print('ok')"],
                             cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "ok", res.stderr)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestToolsStart(unittest.TestCase):
    """Every curses tool, launched with a language this machine lacks. HOME is a temp folder, so nothing real is touched."""

    TOOLS = {"kvctl": [], "trctl": ["watch"], "plexsort": [], "syswatch": [], "bookread": []}

    def test_they_start(self):
        for tool, args in self.TOOLS.items():
            with self.subTest(tool):
                self.check(tool, args)

    def check(self, tool, args):
        home = tempfile.mkdtemp(prefix="kutil-test-")
        self.addCleanup(shutil.rmtree, home, True)
        session = f"locale-{tool}-{os.getpid()}"
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", session], capture_output=True)
        env = {"HOME": home, "XDG_CONFIG_HOME": f"{home}/config", "XDG_STATE_HOME": f"{home}/state", "XDG_DATA_HOME": f"{home}/data",
               "TRCTL_URL": "http://127.0.0.1:9/transmission/rpc", "LC_ALL": BAD_LOCALE, "TERM": "xterm-256color"}
        prefix = " ".join(f"{k}='{v}'" for k, v in env.items())
        command = f"{prefix} {sys.executable} '{ROOT / 'bin' / tool}' {' '.join(args)} 2>'{home}/err'"
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "100", "-y", "30", "bash", "-c", command], check=True)
        time.sleep(1.2)
        with open(f"{home}/err") as f:
            self.assertNotIn("locale.Error", f.read())


if __name__ == "__main__":
    unittest.main()
