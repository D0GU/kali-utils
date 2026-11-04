"""Wrapping text to a width, splitting it into pages, and finding a word again after the window changes."""

from __future__ import annotations

import os
import unittest

from bookread import ebook
from bookread.ebook import BOLD, ITALIC, cell_width
from bookread.pages import Layout
from tests.bookread.helpers import Workspace, make_book


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)

    def book(self, chapters) -> ebook.Book:
        if isinstance(chapters, str):
            chapters = [("a.xhtml", chapters)]
        return ebook.load(make_book(os.path.join(self.ws.root, "b.epub"), chapters))

    def rows(self, body, width, rows=1000) -> list[str]:
        """The text of each line on the first page, with its indent or centring as spaces."""
        layout = Layout(self.book(body), width, rows)
        return [" " * x + text for x, text in ((l.x, layout.text(l)) for l in layout.page_lines(0))]


class TestWrap(Base):
    def test_words_wrap_at_the_width(self):
        self.assertEqual(self.rows("<p>aaa bbb ccc ddd</p>", 7), ["aaa bbb", "ccc ddd"])
        self.assertEqual(self.rows("<p>aaa bbb ccc ddd</p>", 3), ["aaa", "bbb", "ccc", "ddd"])
        self.assertEqual(self.rows("<p>aaa bbb</p>", 6), ["aaa", "bbb"])

    def test_a_word_wider_than_the_page_is_cut(self):
        self.assertEqual(self.rows("<p>abcdefgh ij</p>", 3), ["abc", "def", "gh", "ij"])

    def test_wide_characters_take_two_cells(self):
        self.assertEqual(self.rows("<p>日本語日本語</p>", 6), ["日本語", "日本語"])
        self.assertEqual(self.rows("<p>日本語日本語</p>", 5), ["日本", "語日", "本語"])

    def test_a_wide_character_on_a_one_cell_line_cannot_loop_forever(self):
        self.assertEqual(self.rows("<p>語</p>", 1), ["語"])

    def test_combining_marks_take_no_cell(self):
        # U+0323 U+0301 has no single composed form with q, so it survives normalising
        word = "q̣́"
        self.assertEqual(self.rows(f"<p>{word} {word}</p>", 3), [f"{word} {word}"])

    def test_paragraphs_have_a_blank_line_between_them(self):
        self.assertEqual(self.rows("<p>a</p><p>b</p>", 10), ["a", "", "b"])

    def test_line_breaks_have_no_blank_line(self):
        self.assertEqual(self.rows("<p>a<br/>b</p>", 10), ["a", "b"])

    def test_headings_and_scene_breaks_are_centred(self):
        self.assertEqual(self.rows("<h1>Head</h1><hr/><p>a</p>", 10), ["   Head", "", "  * * *", "", "a"])

    def test_a_wrapped_heading_centres_each_line(self):
        self.assertEqual(self.rows("<h1>aa bbbb</h1>", 5), [" aa", "bbbb"])

    def test_indented_text_has_less_room(self):
        self.assertEqual(self.rows("<blockquote><p>aaaa bbbb</p></blockquote>", 10), ["  aaaa", "  bbbb"])

    def test_indent_deeper_than_the_page_still_leaves_room_for_text(self):
        deep = "<blockquote>" * 10 + "<p>abc</p>" + "</blockquote>" * 10
        for line in self.rows(deep, 6):
            self.assertLessEqual(cell_width(line), 6)
            self.assertTrue(line.strip())

    def test_preformatted_spacing_is_kept(self):
        self.assertEqual(self.rows("<pre>a   b</pre>", 10), ["a   b"])

    def test_nothing_is_lost_or_too_wide(self):
        body = "<p>Mixed 日本語 text with a verylongwordthatmustbecut and <em>some style</em> plus café.</p><ul><li>one item</li></ul>"
        for width in (4, 5, 7, 11, 13, 40):
            with self.subTest(width):
                book = self.book(body)
                layout = Layout(book, width, 1000)
                self.assertLessEqual(max(l.x + cell_width(layout.text(l)) for l in layout.lines), width)
                joined = "".join(layout.text(l) for l in layout.lines).replace(" ", "")
                self.assertEqual(joined, "".join(b.text for b in book.blocks).replace(" ", ""))


class TestStyles(Base):
    def segments(self, body, width):
        layout = Layout(self.book(body), width, 1000)
        return [layout.segments(l) for l in layout.lines]

    def test_a_style_follows_its_words_across_a_line_end(self):
        self.assertEqual(self.segments("<p>aa <em>bb cc</em> dd</p>", 5), [[("aa ", 0), ("bb", ITALIC)], [("cc", ITALIC), (" dd", 0)]])

    def test_plain_lines_are_one_piece(self):
        self.assertEqual(self.segments("<p>aa</p>", 5), [[("aa", 0)]])

    def test_neighbouring_styles_stay_apart(self):
        self.assertEqual(self.segments("<p><b>a</b><i>b</i>c</p>", 5), [[("a", BOLD), ("b", ITALIC), ("c", 0)]])

    def test_blank_lines_have_nothing_to_draw(self):
        self.assertEqual(self.segments("<p>a</p><p>b</p>", 5)[1], [])


class TestPages(Base):
    PARAS = "".join(f"<p>w{i}</p>" for i in range(10))

    def test_pages_hold_as_many_rows_as_fit(self):
        layout = Layout(self.book(self.PARAS), 10, 3)
        self.assertEqual([layout.text(l) for l in layout.page_lines(0)], ["w0", "", "w1"])
        self.assertEqual([layout.text(l) for l in layout.page_lines(1)], ["w2", "", "w3"])
        self.assertEqual(layout.pages, 5)

    def test_no_page_starts_with_a_blank_line(self):
        layout = Layout(self.book(self.PARAS), 10, 2)
        for page in range(layout.pages):
            self.assertTrue(layout.text(layout.page_lines(page)[0]))

    def test_a_paragraph_longer_than_a_page_continues_on_the_next(self):
        layout = Layout(self.book("<p>a b c d e</p>"), 1, 2)
        self.assertEqual([[layout.text(l) for l in layout.page_lines(p)] for p in range(layout.pages)], [["a", "b"], ["c", "d"], ["e"]])

    def test_each_file_starts_a_page(self):
        layout = Layout(self.book([("a.xhtml", "<p>a</p>"), ("b.xhtml", "<p>b</p>")]), 10, 100)
        self.assertEqual(layout.pages, 2)
        self.assertEqual([layout.text(l) for l in layout.page_lines(1)], ["b"])

    def test_a_heading_is_not_left_alone_at_the_bottom(self):
        layout = Layout(self.book("<p>a</p><h2>Head</h2><p>b</p>"), 10, 3)
        self.assertEqual([layout.text(l) for l in layout.page_lines(0)], ["a"])
        self.assertEqual([layout.text(l) for l in layout.page_lines(1)], ["Head", "", "b"])

    def test_a_page_of_only_headings_still_moves_on(self):
        layout = Layout(self.book("".join(f"<h2>h{i}</h2>" for i in range(5))), 10, 2)
        seen = [layout.text(l) for p in range(layout.pages) for l in layout.page_lines(p) if layout.text(l)]
        self.assertEqual(seen, ["h0", "h1", "h2", "h3", "h4"])

    def test_word_ranges_of_pages(self):
        layout = Layout(self.book(self.PARAS), 10, 3)     # two words a page
        self.assertEqual([(layout.start_word(p), layout.end_word(p)) for p in range(layout.pages)],
                         [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)])

    def test_finding_the_page_of_a_word(self):
        layout = Layout(self.book(self.PARAS), 10, 3)
        table = {0: 0, 1: 0, 2: 1, 9: 4, 10_000: 4, -5: 0}
        for word, page in table.items():
            with self.subTest(word):
                self.assertEqual(layout.page_of(word), page)

    def test_a_word_cut_across_pages_resumes_on_its_first_part(self):
        layout = Layout(self.book("<p>abcdef g</p>"), 2, 1)
        self.assertEqual(layout.page_of(0), 0)

    def test_every_word_is_on_the_page_found_for_it_at_any_size(self):
        book = self.book("".join(f"<p>{'word ' * (i % 7 + 1)}</p>" for i in range(40)))
        for width, rows in ((20, 5), (11, 3), (80, 24), (5, 2)):
            layout = Layout(book, width, rows)
            for word in range(book.words):
                page = layout.page_of(word)
                self.assertLessEqual(layout.start_word(page), word)
                self.assertLess(word, layout.end_word(page))


if __name__ == "__main__":
    unittest.main()
