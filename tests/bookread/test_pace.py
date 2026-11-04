"""Reading speed from page turns, and the progress stored between sessions."""

from __future__ import annotations

import os
import unittest

from bookread.pace import Pace, human_duration
from bookread.store import Store as DB
from tests.bookread.helpers import Workspace


class TestPace(unittest.TestCase):
    def test_a_new_reader_is_predicted_at_the_global_average(self):
        self.assertEqual(Pace().seconds_for(238), 60)

    def test_one_page_moves_the_estimate_by_one_part_in_twenty_one(self):
        pace = Pace()
        self.assertTrue(pace.record(500, 60))      # 500 wpm, against 238 worth 20 pages
        self.assertAlmostEqual(pace.wpm, 238 + (500 - 238) / 21)

    def test_a_steady_reader_pulls_the_estimate_to_their_speed(self):
        pace = Pace()
        for _ in range(300):
            pace.record(400, 60)
        self.assertGreater(pace.wpm, 385)
        self.assertLessEqual(pace.wpm, 400)

    def test_old_pages_fade_so_a_change_of_speed_shows_up(self):
        pace = Pace()
        for _ in range(2000):
            pace.record(200, 60)
        for _ in range(200):
            pace.record(400, 60)
        self.assertGreater(pace.wpm, 310)   # with a plain average over 2200 pages it would still be near 218

    def test_turns_that_measure_no_reading_are_ignored(self):
        cases = {"flipping through": (300, 2), "walked away": (300, 3600), "nearly empty page": (10, 3),
                 "no time passed": (300, 0)}
        for name, (words, seconds) in cases.items():
            with self.subTest(name):
                pace = Pace()
                self.assertFalse(pace.record(words, seconds))
                self.assertEqual((pace.wpm, pace.samples), (238, 0))

    def test_speed_survives_between_sessions(self):
        db = DB(":memory:")
        self.addCleanup(db.close)
        pace = Pace.load(db)
        self.assertEqual(pace.wpm, 238)
        pace.record(500, 60)
        pace.save(db)
        again = Pace.load(db)
        self.assertAlmostEqual(again.wpm, pace.wpm)
        self.assertEqual(again.samples, 1)


class TestDuration(unittest.TestCase):
    def test_durations(self):
        table = [(0, "<1m"), (29, "<1m"), (60, "1m"), (90, "2m"), (3540, "59m"), (3600, "1h"), (3660, "1h 1m"),
                 (12000, "3h 20m"), (3599, "1h"), (360000, "100h")]
        for seconds, want in table:
            with self.subTest(seconds):
                self.assertEqual(human_duration(seconds), want)


class TestProgressStore(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.path = self.ws.database
        self.db = DB(self.path)
        self.addCleanup(self.db.close)

    def test_a_book_never_opened_has_no_progress(self):
        self.assertIsNone(self.db.reading_get("56"))

    def test_progress_survives_reopening_and_the_latest_save_wins(self):
        self.db.reading_save("56", 100, 1000, 0.12)
        self.db.reading_save("56", 200, 1000, 0.22)
        self.db.close()
        self.db = DB(self.path)
        row = self.db.reading_get("56")
        self.assertEqual((row["position"], row["words"], row["percent"]), (200, 1000, 0.22))

    def test_all_progress_is_listed_per_book(self):
        self.db.reading_save("56", 100, 1000, 0.12)
        self.db.reading_save("57", 5, 50, 0.2)
        self.assertEqual({k: v["position"] for k, v in self.db.reading_all().items()}, {"56": 100, "57": 5})

    def test_a_books_length_is_remembered(self):
        self.assertEqual(self.db.lengths(), {})
        self.db.length_save("abc", 1234)
        self.assertEqual(self.db.lengths(), {"abc": 1234})

    def test_settings_read_back_and_default_when_missing(self):
        self.assertEqual(self.db.meta_get("reader_theme", "paper"), "paper")
        self.db.meta_set("reader_theme", "sepia")
        self.assertEqual(self.db.meta_get("reader_theme", "paper"), "sepia")


if __name__ == "__main__":
    unittest.main()
