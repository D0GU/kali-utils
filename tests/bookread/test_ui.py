"""Drive the real `bookread` inside a detached tmux session, in a terminal with 256 colours."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from bookread.library import book_key
from bookread.store import Store as DB
from tests.bookread.helpers import Workspace, make_book, write_file
from tests.bookread.test_ebook import ncx, point

ROOT = Path(__file__).resolve().parents[2]


def chapter(name: str, paragraphs: int = 40) -> str:
    return f"<h1>{name}</h1>" + "".join(f"<p>{name}para{i} " + "lorem ipsum dolor sit amet " * 8 + "</p>" for i in range(paragraphs))


class UI(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.session = f"kvread-test-{os.getpid()}"
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", self.session], capture_output=True)
        self.env = {"BOOKREAD_CONFIG": self.ws.config, "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        self.long = self.add_book("Long Road", "Ann Lee", 10, [("a.xhtml", chapter("Alpha")), ("b.xhtml", chapter("Beta"))],
                                  ncx=ncx(point("Alpha", "a.xhtml"), point("Beta", "b.xhtml")))
        self.short = self.add_book("Short Walk", "Bob Ray", 11, [("a.xhtml", chapter("Solo", 3))])

    def add_book(self, title: str, author: str, book_id: int, chapters, **kw) -> str:
        path = f"{self.ws.books}/{author}/{title} ({book_id})/{title} - {author}.epub"
        return make_book(path, chapters, title=title, authors=(author,), **kw)

    def add_mobi(self, title: str, author: str) -> str:
        return write_file(f"{self.ws.books}/{author}/{title}/{title} - {author}.mobi", b"x")

    def start(self, *args, cols=100, rows=30) -> None:
        env = " ".join(f"{k}='{v}'" for k, v in self.env.items())
        quoted = " ".join("'" + a + "'" for a in args)
        command = f"{env} exec {sys.executable} '{ROOT / 'bin' / 'bookread'}' {quoted}"
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session, "-x", str(cols), "-y", str(rows), "bash", "-c", command], check=True)

    def keys(self, *keys) -> None:
        for k in keys:
            subprocess.run(["tmux", "send-keys", "-t", self.session, k], check=True)
            time.sleep(0.1)

    def screen(self, colours: bool = False) -> str:
        args = ["-e"] if colours else []
        return subprocess.run(["tmux", "capture-pane", "-p", *args, "-t", self.session], capture_output=True, text=True).stdout

    def wait_for(self, text: str, timeout: float = 6.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            if text in (s := self.screen()):
                return s
            time.sleep(0.05)
        self.fail(f"{text!r} never appeared:\n{self.screen()}")

    def wait_gone(self, text: str, timeout: float = 6.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            if text not in (s := self.screen()):
                return s
            time.sleep(0.05)
        self.fail(f"{text!r} never went away:\n{self.screen()}")

    def wait_exit(self) -> None:
        end = time.time() + 5
        while time.time() < end:
            if subprocess.run(["tmux", "has-session", "-t", self.session], capture_output=True).returncode:
                return
            time.sleep(0.05)
        self.fail(f"still running:\n{self.screen()}")

    def stored(self, book: str):
        db = DB(self.ws.database)
        try:
            return db.reading_get(book_key(book))
        finally:
            db.close()

    def left_edge(self, marker: str) -> int:
        line = next(l for l in self.screen().splitlines() if marker in l)
        return len(line) - len(line.lstrip())


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestShelf(UI):
    def test_choose_read_and_come_back_to_see_progress(self):
        self.start()
        screen = self.wait_for("Long Road")
        self.assertIn("Short Walk", screen)
        self.assertIn("238 words a minute", screen)
        self.keys("Enter")
        self.wait_for("Alphapara0")
        self.keys("Space")
        self.wait_for("p. 2/")
        self.keys("q")
        screen = self.wait_for("Read")
        row = next(l for l in screen.splitlines() if "Long Road" in l)
        self.assertRegex(row, r"\d+%\s+\d+m")
        self.keys("q")
        self.wait_exit()

    def test_the_menu_shows_how_long_unopened_books_are(self):
        self.start()
        self.wait_for("Short Walk")
        end = time.time() + 5
        while time.time() < end and not re.search(r"Long Road.*\s-\s+\d+m", self.screen()):
            time.sleep(0.05)
        self.assertRegex(self.screen(), r"Long Road.*\s-\s+\d+m")

    def test_opening_a_finished_book_says_how_to_start_again(self):
        self.start("short walk", rows=100)
        self.wait_for("Solopara0")
        self.keys("Space")
        self.wait_for("End of the book")
        self.keys("q")
        self.wait_exit()
        self.start("short walk", rows=100)
        self.wait_for("Home starts it again")

    def test_a_narrow_long_list_keeps_progress_and_its_headings(self):
        for n in range(30):
            self.add_book(f"Filler {n}", "Ann Lee", 100 + n, [("a.xhtml", "<p>x</p>")])
        self.start(cols=60, rows=20)
        screen = self.wait_for("Filler 1")
        header = next(l for l in screen.splitlines() if l.startswith("Title"))
        self.assertIn("Read", header)
        self.assertIn("Left", header)

    def test_searching_in_a_narrow_window_does_not_crash(self):
        self.start(cols=30, rows=12)
        self.wait_for("Long Road")
        self.keys("/", "a" * 40)
        time.sleep(0.2)
        self.assertIn("bookread", self.screen())
        self.keys("Escape", "q")
        self.wait_exit()

    def test_a_folder_argument_lists_that_folder(self):
        make_book(f"{self.ws.root}/elsewhere/Far Away.epub", [("a.xhtml", chapter("Far", 2))], title="Far Away")
        self.start(f"{self.ws.root}/elsewhere")
        screen = self.wait_for("Far Away")
        self.assertNotIn("Long Road", screen)

    def test_an_empty_folder_says_how_to_choose_another(self):
        os.makedirs(f"{self.ws.root}/empty")
        self.start(f"{self.ws.root}/empty")
        self.wait_for("--dir")

    def test_several_matches_open_the_menu_narrowed_to_them(self):
        self.add_book("Long Pier", "Dee Ash", 13, [("a.xhtml", chapter("Pier", 2))])
        self.start("long")
        self.wait_for("Long Pier")
        screen = self.wait_for("Long Road")
        self.assertNotIn("Short Walk", screen)

    def test_the_book_last_read_moves_to_the_top(self):
        self.start()
        self.wait_for("Short Walk")
        self.keys("j", "Enter")
        self.wait_for("Solopara0")
        self.keys("q")
        self.wait_for("Short Walk")
        rows = [l for l in self.screen().splitlines() if "Long Road" in l or "Short Walk" in l]
        self.assertIn("Short Walk", rows[0])

    def test_search_narrows_the_list(self):
        self.start()
        self.wait_for("Long Road")
        self.keys("/", "short", "Enter")
        self.wait_gone("Long Road")
        self.keys("Enter")
        self.wait_for("Solopara0")

    def test_a_book_in_another_format_says_why_it_cannot_open(self):
        self.add_mobi("Aaa Mobi", "Cal Poe")
        self.start()
        self.wait_for("Aaa Mobi")
        rows = [l for l in self.screen().splitlines() if any(t in l for t in ("Aaa Mobi", "Long Road", "Short Walk"))]
        at = next(i for i, l in enumerate(rows) if "Aaa Mobi" in l)
        self.keys(*["j"] * at, "Enter")
        self.wait_for("The reader opens EPUB")

    def test_a_damaged_book_reports_and_the_list_keeps_working(self):
        write_file(self.short, b"not a zip")
        self.start()
        self.wait_for("Short Walk")
        self.keys("j", "Enter")
        self.wait_for("can't read this book")
        self.keys("k", "Enter")
        self.wait_for("Alphapara0")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestReading(UI):
    def test_a_file_anywhere_opens_without_a_library(self):
        path = make_book(f"{self.ws.root}/loose/book.epub", [("a.xhtml", chapter("Loose", 2))], title="Loose")
        self.start(path)
        self.wait_for("Loosepara0")
        self.keys("q")
        self.wait_exit()
        self.assertIsNotNone(self.stored(path))

    def test_opening_by_name_turning_pages_and_resuming(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("Space", "Space")
        self.wait_for("p. 3/")
        top = self.stored(self.long)["position"]
        self.assertGreater(top, 0)
        self.keys("Left")
        self.wait_for("p. 2/")
        self.keys("q")
        self.wait_exit()
        self.assertLess(self.stored(self.long)["position"], top)

        self.start("long road")
        self.wait_for("p. 2/")

    def test_status_line_shows_progress_and_time_left(self):
        self.start("long road")
        screen = self.wait_for("chapter ·")
        self.assertRegex(screen, r"Alpha · p\. 1/\d+ · \d+%")
        self.assertRegex(screen, r"\d+m chapter · (\d+h )?\d+m book")

    def test_key_help_names_the_terminal_zoom_and_the_speed(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("?")
        screen = self.wait_for("TEXT SIZE")
        self.assertIn("Ctrl+=", screen)
        self.assertIn("238 words a minute", screen)
        self.keys("Escape")
        self.wait_gone("TEXT SIZE")

    def test_the_keys_hint_stops_once_help_has_been_seen(self):
        self.start("long road")
        self.wait_for("? keys")
        self.keys("?")
        self.wait_for("TEXT SIZE")
        self.keys("Escape", "q")
        self.wait_exit()
        self.start("long road")
        self.wait_for("Alphapara0")
        self.assertNotIn("? keys", self.screen())
        self.assertIn("chapter ·", self.screen())

    def test_contents_jumps_to_a_chapter(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("c")
        screen = self.wait_for("Contents")
        self.assertRegex(screen, r"Alpha\s+0%")
        self.assertRegex(screen, r"Beta\s+50%")     # how far through the book each chapter starts
        self.keys("j", "Enter")
        self.wait_for("Betapara0")
        self.wait_for("Beta · p.")

    def test_italic_and_bold_reach_the_terminal(self):
        self.add_book("Styled", "Eve Fox", 14, [("a.xhtml", "<p>plain <em>slanted</em> <b>heavy</b></p>")])
        self.start("styled")
        self.wait_for("slanted")
        coloured = self.screen(colours=True)
        self.assertRegex(coloured, r"\x1b\[(\d+;)*3(;\d+)*mslanted")
        self.assertRegex(coloured, r"\x1b\[(\d+;)*1(;\d+)*mheavy")

    def test_no_color_keeps_the_terminals_own_colours(self):
        self.env["NO_COLOR"] = "1"
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("t")
        self.wait_for("Theme: Terminal")
        self.assertNotIn("48;5;", self.screen(colours=True))

    def test_an_8_colour_terminal_gets_the_themes_it_can_show(self):
        self.env["TERM"] = "screen"
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("t")
        self.wait_for("Theme: Paper")
        self.assertIn("47m", self.screen(colours=True))
        self.keys("t")
        self.wait_for("Theme: Black")

    def test_a_saved_theme_the_terminal_cannot_show_falls_back(self):
        db = DB(self.ws.database)
        db.meta_set("reader_theme", "sepia")
        db.close()
        self.env["TERM"] = "screen"
        self.start("long road")
        self.wait_for("Alphapara0")
        self.assertNotIn("230", self.screen(colours=True))

    def test_themes_change_the_colours_and_are_remembered(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("t")
        self.wait_for("Theme: Paper")
        self.assertIn("48;5;255", self.screen(colours=True))
        self.keys("t")
        self.wait_for("Theme: Sepia")
        self.assertIn("48;5;230", self.screen(colours=True))
        self.keys("q")
        self.wait_exit()
        self.start("short walk")
        self.wait_for("Solopara0")
        self.assertIn("48;5;230", self.screen(colours=True))

    def test_margins_move_the_text(self):
        self.start("long road", cols=100)
        self.wait_for("Alphapara0")
        self.assertEqual(self.left_edge("Alphapara0"), 14)     # 72 wide in 100 columns
        self.keys("]")
        self.wait_for("lines 68 wide")
        self.assertEqual(self.left_edge("Alphapara0"), 16)
        self.keys("[", "[")
        self.wait_for("lines 76 wide")
        self.assertEqual(self.left_edge("Alphapara0"), 12)

    def test_a_burst_of_arrows_from_a_scroll_wheel_turns_one_page(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Down", "Down", "Down"], check=True)
        self.wait_for("p. 2/")
        time.sleep(0.3)
        self.assertIn("p. 2/", self.screen())
        self.keys("Down")
        time.sleep(0.1)
        self.keys("Down")
        self.wait_for("p. 4/")      # separate presses all count

    def test_an_unknown_key_sequence_does_not_close_the_book(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        subprocess.run(["tmux", "send-keys", "-t", self.session, "-l", "\x1b[9;9~"], check=True)
        time.sleep(0.3)
        self.assertIn("Alphapara0", self.screen())
        self.keys("Escape")
        self.wait_exit()

    def test_keys_do_nothing_while_the_window_is_too_small(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "18", "-y", "20"], check=True)
        self.wait_for("Too small")
        self.keys("Space", "Space")
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "30"], check=True)
        self.wait_for("p. 1/")

    def test_an_unavailable_language_setting_still_runs(self):
        self.env["LC_ALL"] = "xx_YY.UTF-8"
        self.start("long road")
        self.wait_for("Alphapara0")

    def test_top_margin_moves_the_text_down(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        def row_of_heading():
            return next(i for i, l in enumerate(self.screen().splitlines()) if "Alpha" in l and "para" not in l)
        before = row_of_heading()
        self.keys("}")
        self.wait_for("margins 2")
        self.assertEqual(row_of_heading(), before + 1)

    def test_changing_size_keeps_the_place(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("Space", "Space", "Space")
        self.wait_for("p. 4/")
        word = next(m.group(0) for m in re.finditer(r"Alphapara\d+", self.screen()))
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "40", "-y", "20"], check=True)
        self.wait_for(word)
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "18", "-y", "20"], check=True)
        self.wait_for("Too small")
        self.keys("Space")     # no layout for this size yet: must not turn or crash
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "30"], check=True)
        self.wait_for(word)

    def test_a_tiny_window_at_startup_can_still_recover(self):
        self.start("long road", cols=18, rows=20)
        self.wait_for("Too small")
        self.keys("Space")
        subprocess.run(["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "30"], check=True)
        self.wait_for("Alphapara0")

    def test_end_of_the_book_is_stated_and_leaves_100_percent(self):
        self.start("short walk", rows=100)
        self.wait_for("Solopara0")
        self.keys("Space")
        self.wait_for("End of the book")
        self.keys("q")
        self.wait_exit()
        self.assertEqual(self.stored(self.short)["percent"], 1.0)

    def test_ctrl_c_leaves_cleanly_with_the_place_saved(self):
        self.start("long road")
        self.wait_for("Alphapara0")
        self.keys("Space")
        self.wait_for("p. 2/")
        self.keys("C-c")
        self.wait_exit()
        self.assertGreater(self.stored(self.long)["position"], 0)


if __name__ == "__main__":
    unittest.main()
