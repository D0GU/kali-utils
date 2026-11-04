"""What a reading session does, without a terminal: page turns, saved places, speed samples, the status line."""

from __future__ import annotations

import os
import unittest

from bookread import ebook, reader
from bookread.library import Entry
from bookread.pace import Pace
from bookread.reader import Prefs, Session, progress_label, shelf_order, view
from bookread.store import Store as DB
from tests.bookread.helpers import Workspace, make_book
from tests.bookread.test_ebook import ncx, point


def words(n: int) -> str:
    return "<p>" + "word " * n + "</p>"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)
        self.clock = Clock()

    def book(self, chapters, **kw) -> ebook.Book:
        return ebook.load(make_book(os.path.join(self.ws.root, "b.epub"), chapters, **kw))

    def four_pages(self) -> ebook.Book:
        """Four files of 300 words, so with room to spare each is exactly one page."""
        return self.book([(f"{c}.xhtml", words(300)) for c in "abcd"])

    def session(self, book=None, key="56") -> Session:
        s = Session(book or self.four_pages(), key, "Book Title", self.db, clock=self.clock)
        s.set_view(72, 1000)
        return s

    def read_for(self, s: Session, seconds: float) -> None:
        self.clock.now += seconds
        s.next_page()


class TestPlace(Base):
    def test_a_new_book_opens_at_the_start_and_is_recorded(self):
        s = self.session()
        self.assertEqual((s.page, s.position), (0, 0))
        self.assertEqual(self.db.reading_get("56")["position"], 0)

    def test_every_page_turn_is_saved(self):
        s = self.session()
        s.next_page()
        row = self.db.reading_get("56")
        self.assertEqual((row["position"], row["words"]), (300, 1200))
        self.assertEqual(row["percent"], 0.5)      # page 2 of 4 is on screen, so half has been reached

    def test_reopening_resumes_where_you_left_off(self):
        book = self.four_pages()
        s = self.session(book)
        s.next_page()
        s.next_page()
        again = self.session(book)
        self.assertEqual((again.page, again.position), (2, 600))

    def test_a_saved_place_past_the_end_lands_on_the_last_page(self):
        self.db.reading_save("56", 99_999, 99_999, 0.9)
        s = self.session()
        self.assertEqual(s.page, 3)

    def test_turning_stops_at_both_ends(self):
        s = self.session()
        s.prev_page()
        self.assertEqual(s.page, 0)
        for _ in range(10):
            s.next_page()
        self.assertEqual(s.page, 3)
        self.assertEqual(self.db.reading_get("56")["percent"], 1.0)

    def test_going_back_is_saved_too(self):
        s = self.session()
        s.next_page()
        s.prev_page()
        self.assertEqual(self.db.reading_get("56")["position"], 0)

    def test_books_keep_separate_places(self):
        book = self.four_pages()
        a = self.session(book, "1")
        self.session(book, "2")
        a.next_page()
        self.assertEqual(self.db.reading_get("2")["position"], 0)

    def test_jumping_goes_to_the_page_holding_that_word(self):
        s = self.session()
        s.go_to(650)
        self.assertEqual((s.page, s.position), (2, 600))
        self.assertEqual(self.db.reading_get("56")["position"], 600)


class TestFinishing(Base):
    def test_the_last_page_on_screen_is_not_yet_finished(self):
        s = self.session()
        s.go_to(900)
        self.assertLess(self.db.reading_get("56")["percent"], 1.0)
        self.assertEqual(s.status(80)[0].split("· ")[-1], "100%")     # but the status line says how far you've got

    def test_turning_past_the_last_page_finishes_it(self):
        s = self.session()
        s.go_to(900)
        s.next_page()
        self.assertEqual(self.db.reading_get("56")["percent"], 1.0)

    def test_a_short_book_is_not_done_just_by_opening_it(self):
        self.session(self.book([("a.xhtml", words(50))]))
        self.assertLess(self.db.reading_get("56")["percent"], 1.0)

    def test_reopening_a_finished_book_keeps_it_finished(self):
        book = self.four_pages()
        s = self.session(book)
        s.go_to(900)
        s.next_page()
        again = self.session(book)
        self.assertTrue(again.finished_before)
        self.assertEqual(self.db.reading_get("56")["percent"], 1.0)

    def test_going_back_from_a_finished_book_clears_it(self):
        book = self.four_pages()
        s = self.session(book)
        s.go_to(900)
        s.next_page()
        s.prev_page()
        self.assertLess(self.db.reading_get("56")["percent"], 1.0)


class TestSaveFailures(Base):
    def test_a_failed_save_is_reported_and_reading_continues(self):
        s = self.session()
        self.db.conn.execute("DROP TABLE reading")     # stands in for a locked or full database
        self.assertTrue(s.next_page())
        self.assertEqual(s.page, 1)
        self.assertIn("reading", s.take_problem())
        self.assertIsNone(s.take_problem())

    def test_a_failed_speed_save_does_not_stop_the_turn(self):
        s = self.session()
        self.db.conn.execute("DROP TABLE meta")
        self.read_for(s, 60)
        self.assertEqual(s.page, 1)
        self.assertIsNotNone(s.take_problem())


class TestResize(Base):
    def long_book(self):
        return self.book([("a.xhtml", "".join(words(40) for _ in range(100)))])

    def test_the_word_at_the_top_stays_on_screen_when_the_size_changes(self):
        s = self.session(self.long_book())
        s.set_view(40, 20)
        for _ in range(7):
            s.next_page()
        top = s.position
        s.set_view(90, 40)
        self.assertLessEqual(s.layout.start_word(s.page), top)
        self.assertLess(top, s.layout.end_word(s.page))

    def test_zooming_in_and_back_out_returns_to_the_same_page(self):
        s = self.session(self.long_book())
        s.set_view(40, 20)
        for _ in range(7):
            s.next_page()
        page, top = s.page, s.position
        s.set_view(30, 12)
        s.set_view(90, 50)
        s.set_view(20, 8)
        s.set_view(40, 20)
        self.assertEqual((s.page, s.position), (page, top))

    def test_a_redraw_at_the_same_size_does_not_lay_out_again(self):
        s = self.session()
        layout = s.layout
        s.set_view(72, 1000)
        self.assertIs(s.layout, layout)


class TestSpeed(Base):
    def test_a_page_turn_teaches_the_speed_and_persists_it(self):
        s = self.session()
        self.read_for(s, 60)    # 300 words in a minute
        want = 238 + (300 - 238) / 21
        self.assertAlmostEqual(s.pace.wpm, want)
        self.assertAlmostEqual(Pace.load(self.db).wpm, want)

    def test_going_back_and_rereading_teaches_nothing(self):
        s = self.session()
        self.read_for(s, 60)
        self.clock.now += 60
        s.prev_page()
        self.read_for(s, 60)     # back on the page already read
        self.read_for(s, 60)     # and leaving it
        self.assertEqual(s.pace.samples, 1)
        self.read_for(s, 60)     # a page not seen before counts again
        self.assertEqual(s.pace.samples, 2)

    def test_flipping_through_and_walking_away_teach_nothing(self):
        s = self.session()
        self.read_for(s, 1)
        self.read_for(s, 7200)
        self.assertEqual(s.pace.samples, 0)
        self.assertEqual(s.pace.wpm, 238)

    def test_changing_the_window_loses_only_that_page(self):
        s = self.session()
        self.clock.now += 30
        s.set_view(71, 1000)
        self.read_for(s, 30)     # half the page was seen under another layout
        self.assertEqual(s.pace.samples, 0)
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 1)

    def test_time_with_a_dialog_open_does_not_count(self):
        s = self.session()
        s.pause()
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 0)
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 1)

    def test_the_last_page_counts_once(self):
        s = self.session()
        s.go_to(900)
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 1)
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 1)

    def test_a_jump_does_not_time_the_page_left_behind(self):
        s = self.session()
        self.clock.now += 60
        s.go_to(600)
        self.assertEqual(s.pace.samples, 0)

    def test_after_jumping_back_pages_are_timed_again(self):
        s = self.session()
        self.read_for(s, 60)
        self.read_for(s, 60)
        s.go_to(0)
        self.read_for(s, 60)
        self.assertEqual(s.pace.samples, 3)

    def test_a_faster_reader_is_told_less_time_left(self):
        s = self.session()
        self.assertAlmostEqual(s.seconds_left(), 1200 / 238 * 60)
        s.pace.wpm = 476
        self.assertAlmostEqual(s.seconds_left(), 1200 / 476 * 60)


class TestStatus(Base):
    def book_with_chapters(self, first="One"):
        return self.book([("a.xhtml", words(238)), ("b.xhtml", words(476))], ncx=ncx(point(first, "a.xhtml"), point("Two", "b.xhtml")))

    def test_widths(self):
        table = [(80, ("One · p. 1/2 · 33%", "1m chapter · 3m book")),
                 (60, ("One · p. 1/2 · 33%", "1m chapter · 3m book")),
                 (40, ("p. 1/2 · 33%", "1m chapter · 3m book")),
                 (30, ("p. 1/2 · 33%", "3m left")),
                 (20, ("p. 1/2 · 33%", ""))]
        s = self.session(self.book_with_chapters())
        for cols, want in table:
            with self.subTest(cols):
                self.assertEqual(s.status(cols), want)

    def test_a_long_chapter_name_is_shortened(self):
        s = self.session(self.book_with_chapters("A very long chapter name indeed"))
        self.assertEqual(s.status(50)[0], "A very lon… · p. 1/2 · 33%")

    def test_the_status_moves_with_the_reader(self):
        s = self.session(self.book_with_chapters())
        s.next_page()
        self.assertEqual(s.status(80), ("Two · p. 2/2 · 100%", "2m chapter · 2m book"))

    def test_without_chapters_it_shows_the_book_and_only_the_total(self):
        s = self.session(self.book([("a.xhtml", words(238))]))
        self.assertEqual(s.status(80), ("Book Title · p. 1/1 · 100%", "1m left"))


class TestView(unittest.TestCase):
    def test_sizes(self):
        table = [((240, 60), Prefs(), (72, 57, 84, 1)),     # a full 1080p window keeps a readable line
                 ((80, 24), Prefs(), (72, 21, 4, 1)),
                 ((60, 24), Prefs(), (56, 21, 2, 1)),        # a narrow split keeps its side margins
                 ((240, 60), Prefs(width=120), (120, 57, 60, 1)),
                 ((80, 24), Prefs(margin_y=0), (72, 23, 4, 0)),
                 ((80, 24), Prefs(margin_y=3), (72, 17, 4, 3)),
                 ((40, 10), Prefs(margin_y=4), (36, 3, 2, 3))]   # big margins give way to at least three rows
        for (cols, rows), prefs, want in table:
            with self.subTest(f"{cols}x{rows} {prefs}"):
                self.assertEqual(view(cols, rows, prefs), want)


class TestPrefs(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.addCleanup(self.db.close)

    def test_margins_change_from_what_is_on_screen(self):
        prefs = Prefs(width=200)
        prefs.change_side_margin(1, 80)       # the window only fits 76, so the margin grows from there
        self.assertEqual(prefs.width, 72)
        prefs.change_side_margin(-1, 80)
        self.assertEqual(prefs.width, 76)
        prefs.change_side_margin(-1, 80)      # already as wide as the window allows
        self.assertEqual(prefs.width, 76)

    def test_a_margin_key_at_the_limit_keeps_the_saved_width(self):
        prefs = Prefs(width=72)
        prefs.change_side_margin(-1, 40)      # zoomed in: nothing can change, so the setting for wider windows stays
        self.assertEqual(prefs.width, 72)

    def test_text_never_narrower_than_the_minimum(self):
        prefs = Prefs(width=22)
        prefs.change_side_margin(1, 200)
        prefs.change_side_margin(1, 200)
        self.assertEqual(prefs.width, reader.MIN_TEXT)

    def test_top_and_bottom_margin_limits(self):
        prefs = Prefs(margin_y=0)
        prefs.change_top_margin(-1)
        self.assertEqual(prefs.margin_y, 0)
        for _ in range(50):
            prefs.change_top_margin(1)
        self.assertEqual(prefs.margin_y, reader.MAX_MARGIN_Y)

    def test_themes_cycle_both_ways(self):
        names = [t.key for t in reader.THEMES]
        prefs = Prefs(theme=names[0])
        prefs.next_theme(1, reader.THEMES)
        self.assertEqual(prefs.theme, names[1])
        prefs.next_theme(-1, reader.THEMES)
        prefs.next_theme(-1, reader.THEMES)
        self.assertEqual(prefs.theme, names[-1])

    def test_settings_apply_to_every_book_and_session(self):
        prefs = Prefs.load(self.db)
        prefs.theme, prefs.width, prefs.margin_y = "sepia", 60, 3
        prefs.save(self.db)
        again = Prefs.load(self.db)
        self.assertEqual((again.theme, again.width, again.margin_y), ("sepia", 60, 3))

    def test_damaged_settings_fall_back_to_defaults(self):
        self.db.meta_set("reader_width", "wide")
        self.db.meta_set("reader_margin_y", "-")
        self.db.meta_set("reader_theme", "gone")
        prefs = Prefs.load(self.db)
        self.assertEqual((prefs.width, prefs.margin_y, prefs.theme), (Prefs().width, Prefs().margin_y, Prefs().theme))


def xterm_rgb(n: int) -> tuple[int, int, int]:
    """The standard colours of the 256-colour cube and grey ramp, independent of the user's scheme."""
    if n >= 232:
        v = 8 + (n - 232) * 10
        return v, v, v
    n -= 16
    steps = (0, 95, 135, 175, 215, 255)
    return steps[n // 36], steps[n // 6 % 6], steps[n % 6]


def contrast(a: int, b: int) -> float:
    def luminance(n):
        out = []
        for c in xterm_rgb(n):
            c /= 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class TestThemes(unittest.TestCase):
    def test_coloured_themes_are_comfortable_to_read(self):
        for t in reader.THEMES:
            if t.fg < 0:
                continue
            with self.subTest(t.key):
                self.assertGreaterEqual(contrast(t.fg, t.bg), 4.5)      # body text
                self.assertGreaterEqual(contrast(t.heading, t.bg), 3)
                self.assertGreaterEqual(contrast(t.status, t.bg), 3)


class TestShelfHelpers(unittest.TestCase):
    def test_labels(self):
        table = [(None, ("-", "-")),
                 ({"position": 0, "words": 2380, "percent": 0.1}, ("10%", "10m")),
                 ({"position": 1190, "words": 2380, "percent": 0.426}, ("42%", "5m")),
                 ({"position": 2370, "words": 2380, "percent": 1.0}, ("done", ""))]
        for row, want in table:
            with self.subTest(row):
                self.assertEqual(progress_label(row, Pace()), want)

    def test_a_book_not_opened_yet_can_show_its_length(self):
        self.assertEqual(progress_label(None, Pace(), 2380), ("-", "10m"))

    def test_recently_read_books_come_first_then_the_rest_in_order(self):
        books = [Entry(f"/b/{k}.epub", k, key=k) for k in "abcd"]
        progress = {"c": {"updated": 100.0}, "d": {"updated": 200.0}}
        self.assertEqual([b.key for b in shelf_order(books, progress)], ["d", "c", "a", "b"])


if __name__ == "__main__":
    unittest.main()
