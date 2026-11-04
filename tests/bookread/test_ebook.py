"""Turning an EPUB's files into text the reader can lay out."""

from __future__ import annotations

import os
import lzma
import unittest
import zipfile
from unittest import mock

from bookread import ebook
from bookread.ebook import BOLD, ITALIC, EbookError
from tests.bookread.helpers import Workspace, make_book, write_file

ADOBE_DRM = "http://www.w3.org/2001/04/xmlenc#aes256-cbc"
FONT_OBFUSCATION = "http://www.idpf.org/2008/embedding"


def ncx(*points: str) -> str:
    return f'<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>{"".join(points)}</navMap></ncx>'


def point(title: str, src: str, *children: str) -> str:
    return f'<navPoint><navLabel><text>{title}</text></navLabel><content src="{src}"/>{"".join(children)}</navPoint>'


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)

    def book(self, chapters, **kw) -> ebook.Book:
        if isinstance(chapters, str):
            chapters = [("a.xhtml", chapters)]
        return ebook.load(make_book(os.path.join(self.ws.root, "b.epub"), chapters, **kw))

    def texts(self, chapters, **kw) -> list[str]:
        return [b.text for b in self.book(chapters, **kw).blocks]


class TestText(Base):
    def test_files_are_read_in_spine_order(self):
        self.assertEqual(self.texts([("z.xhtml", "<p>first</p>"), ("a.xhtml", "<p>second</p>")]), ["first", "second"])

    def test_files_marked_outside_the_reading_order_are_left_out(self):
        self.assertEqual(self.texts([("a.xhtml", "<p>in</p>"), ("b.xhtml", "<p>out</p>")], unlisted=("b.xhtml",)), ["in"])

    def test_whitespace_collapses_and_entities_decode(self):
        self.assertEqual(self.texts("<p>  Fish \n\t &amp;&nbsp;chips &#8212; caf&eacute;  </p>"), ["Fish & chips — café"])

    def test_text_outside_a_paragraph_still_counts(self):
        self.assertEqual(self.texts("loose<p>inside</p>tail<div>end</div>"), ["loose", "inside", "tail", "end"])

    def test_the_page_title_and_code_are_not_book_text(self):
        doc = "<html><head><title>T</title><style>p {color: red}</style></head><body><script>var a = 1;</script><p>real</p></body></html>"
        self.assertEqual(self.texts([("a.xhtml", doc)]), ["real"])

    def test_an_empty_id_attribute_is_harmless(self):
        self.assertEqual(self.texts('<p id="">a</p><a id="">b</a>'), ["a", "b"])

    def test_cell_widths(self):
        table = [("abc", 3), ("日本", 4), ("caf\u00e9", 4), ("q\u0323", 1), ("\u0e01\u0e34", 1), ("\u2764\ufe0f", 1), ("😀", 2)]
        for text, want in table:
            with self.subTest(text):
                self.assertEqual(ebook.cell_width(text), want)

    def test_empty_paragraphs_disappear(self):
        self.assertEqual(self.texts("<p>a</p><p>&nbsp;</p><p> </p><p>b</p>"), ["a", "b"])

    def test_invisible_and_control_characters_are_removed(self):
        # \x1b would otherwise reach the terminal; a soft hyphen would show as a stray mark in some fonts
        self.assertEqual(self.texts("<p>sof­t​ \x1b[31mred\x07</p>"), ["soft [31mred"])

    def test_decomposed_accents_are_composed(self):
        self.assertEqual(self.texts("<p>café</p>"), ["café"])

    def test_a_declared_old_encoding_is_honoured(self):
        doc = '<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>caf\xe9</p></body></html>'.encode("latin-1")
        self.assertEqual(self.texts([("a.xhtml", doc)]), ["café"])

    def test_undeclared_bad_bytes_do_not_stop_the_book(self):
        self.assertEqual(self.texts([("a.xhtml", b"<html><body><p>ok \xff</p></body></html>")]), ["ok �"])

    def test_words_are_numbered_through_the_whole_book(self):
        book = self.book([("a.xhtml", "<p>one two</p><p>three</p>"), ("b.xhtml", "<p>four five six</p>")])
        self.assertEqual([b.word for b in book.blocks], [0, 2, 3])
        self.assertEqual(book.words, 6)

    def test_words_joined_by_inline_tags_stay_one_word(self):
        book = self.book('<p><span>T</span>he cat<b>s,</b> sat</p>')
        self.assertEqual(book.blocks[0].text, "The cats, sat")
        self.assertEqual(book.words, 3)

    def test_each_wide_character_can_be_wrapped_on_its_own(self):
        self.assertEqual(self.book("<p>日本語 abc</p>").words, 4)


class TestStyles(Base):
    def spans(self, body, css=""):
        return self.book(body, css=css).blocks[0].spans

    def test_inline_tags(self):
        table = [("<p><em>ab</em> cd</p>", [(0, 2, ITALIC)]), ("<p><i>ab</i> cd</p>", [(0, 2, ITALIC)]), ("<p>ab <b>cd</b></p>", [(3, 5, BOLD)]),
                 ("<p>ab <strong>cd</strong></p>", [(3, 5, BOLD)]), ("<p><b><i>ab</i></b></p>", [(0, 2, BOLD | ITALIC)]), ("<p>plain</p>", [])]
        for body, want in table:
            with self.subTest(body):
                self.assertEqual(self.spans(body), want)

    def test_spans_line_up_after_whitespace_collapses(self):
        block = self.book("<p>  one   <em>  two  </em>  three </p>").blocks[0]
        self.assertEqual(block.text, "one two three")
        start, end, style = block.spans[0]
        self.assertEqual((block.text[start:end].strip(), style), ("two", ITALIC))

    def test_style_attribute(self):
        self.assertEqual(self.spans('<p><span style="font-style: italic">ab</span></p>'), [(0, 2, ITALIC)])
        self.assertEqual(self.spans('<p><span style="font-weight:700">ab</span></p>'), [(0, 2, BOLD)])

    def test_stylesheet_classes_as_calibre_writes_them(self):
        css = ".calibre5 { display: block; font-style: italic } span.calibre9, .other {font-weight: bold} .plain {color: red}"
        self.assertEqual(self.spans('<p class="calibre5">ab</p>', css), [(0, 2, ITALIC)])
        self.assertEqual(self.spans('<p><span class="x calibre9">ab</span></p>', css), [(0, 2, BOLD)])
        self.assertEqual(self.spans('<p class="plain">ab</p>', css), [])

    def test_style_ends_with_its_tag_even_if_another_closes_late(self):
        self.assertEqual(self.spans("<p><i>ab<b>cd</i>ef</p>"), [(0, 2, ITALIC), (2, 4, ITALIC | BOLD)])

    def test_hidden_classes_are_not_shown(self):
        self.assertEqual(self.texts('<p>a</p><p class="h">secret</p>', css=".h {display:none}"), ["a"])


class TestBlocks(Base):
    def test_kinds(self):
        blocks = self.book("<h2>Head</h2><p>para</p><hr/><blockquote><p>quote</p></blockquote><ul><li>item</li></ul>").blocks
        self.assertEqual([(b.kind, b.text, b.indent) for b in blocks],
                         [("h", "Head", 0), ("p", "para", 0), ("break", "* * *", 0), ("p", "quote", 2), ("p", "• item", 2)])

    def test_numbered_lists_count(self):
        self.assertEqual(self.texts("<ol><li>a</li><li>b</li></ol>"), ["1. a", "2. b"])

    def test_list_items_indent_by_how_deep_they_are(self):
        table = [("<ol><li>a</li></ol>", [("1. a", 2)]), ("<ul><li>a</li></ul>", [("• a", 2)]),
                 ("<ul><li>a<ol><li>b</li></ol></li><li>c</li></ul>", [("• a", 2), ("1. b", 4), ("• c", 2)]),
                 ("<ul><li><p>wrapped</p></li></ul><p>after</p>", [("• wrapped", 2), ("after", 0)])]
        for body, want in table:
            with self.subTest(body):
                self.assertEqual([(b.text, b.indent) for b in self.book(body).blocks], want)

    def test_scene_breaks_are_not_words(self):
        self.assertEqual(self.book("<p>a</p><hr/><p>b</p>").words, 2)

    def test_line_breaks_start_a_line_with_no_gap(self):
        blocks = self.book("<p>Roses are red,<br/>violets are blue</p>").blocks
        self.assertEqual([(b.text, b.tight) for b in blocks], [("Roses are red,", False), ("violets are blue", True)])

    def test_preformatted_text_keeps_its_spacing(self):
        blocks = self.book("<pre>a   b\n  c</pre>").blocks
        self.assertEqual([(b.text, b.indent) for b in blocks], [("a   b", 0), ("c", 2)])

    def test_images_leave_a_note(self):
        self.assertEqual(self.texts('<p><img src="a.png" alt="Map"/></p><p><img src="b.png"/></p><p>x</p>'), ["[image: Map]", "[image]", "x"])

    def test_table_rows_stay_on_one_line(self):
        self.assertEqual(self.texts("<table><tr><td>Name</td><td>Age</td></tr><tr><td>Ann</td><td>3</td></tr></table>"),
                         ["Name · Age", "Ann · 3"])

    def test_each_file_starts_a_new_page(self):
        blocks = self.book([("a.xhtml", "<p>a</p><p>b</p>"), ("b.xhtml", "<p>c</p>")]).blocks
        self.assertEqual([b.new_page for b in blocks], [True, False, True])


class TestOrnaments(Base):
    """Books often use one small picture over and over as a decoration, which says nothing as `[image]`."""
    O = '<p><img src="images/o.png"/></p>'

    def kinds(self, body):
        return [b.text for b in self.book(body).blocks]

    def test_a_picture_used_many_times_between_paragraphs_is_a_scene_break(self):
        self.assertEqual(self.kinds(f"<p>a</p>{self.O}<p>b</p>{self.O}<p>c</p>{self.O}<p>d</p>"), ["a", "* * *", "b", "* * *", "c", "* * *", "d"])

    def test_scene_breaks_from_ornaments_are_not_words(self):
        self.assertEqual(self.book(f"<p>a</p>{self.O}<p>b</p>{self.O}<p>c</p>{self.O}<p>d</p>").words, 4)

    def test_a_picture_used_twice_is_still_a_picture(self):
        self.assertEqual(self.kinds(f"<p>a</p>{self.O}<p>b</p>{self.O}<p>c</p>"), ["a", "[image]", "b", "[image]", "c"])

    def test_no_break_beside_a_heading_or_at_either_end_of_a_file(self):
        chapters = [("a.xhtml", f"{self.O}<h1>One</h1>{self.O}<p>a</p>{self.O}"), ("b.xhtml", f"{self.O}<p>b</p>{self.O}<h1>Two</h1><p>c</p>")]
        self.assertEqual(self.texts(chapters), ["One", "a", "b", "Two", "c"])

    def test_neighbouring_ornaments_make_one_break(self):
        self.assertEqual(self.kinds(f"<p>a</p>{self.O}{self.O}{self.O}<p>b</p>"), ["a", "* * *", "b"])

    def test_an_ornament_inside_a_sentence_is_dropped(self):
        pic = '<img src="o.png"/>'
        self.assertEqual(self.kinds(f"<p>a{pic} b</p><p>{pic}c</p><p>d{pic}</p>"), ["a b", "c", "d"])

    def test_the_same_file_counts_however_it_is_reached(self):
        chapters = [("x/a.xhtml", '<p>a</p><p><img src="../i/o.png"/></p><p>b</p>'), ("b.xhtml", '<p>c</p><p><img src="i/o.png"/></p><p>d</p>'),
                    ("y/c.xhtml", '<p>e</p><p><img src="../i/o.png"/></p><p>f</p>')]
        self.assertEqual(self.texts(chapters), ["a", "* * *", "b", "c", "* * *", "d", "e", "* * *", "f"])

    def test_svg_images_count(self):
        pic = '<svg><image xlink:href="o.png"/></svg>'
        self.assertEqual(self.kinds(f"<p>a</p>{pic}<p>b</p>{pic}<p>c</p>{pic}<p>d</p>"), ["a", "* * *", "b", "* * *", "c", "* * *", "d"])


class TestContents(Base):
    CHAPTERS = [("one.xhtml", "<p>a b c</p>"), ("two.xhtml", "<p>d e</p><p id='mid'>f g</p>"), ("three.xhtml", "<p>h</p>")]

    def entries(self, book):
        return [(c.title, c.word, c.depth) for c in book.chapters]

    def test_ncx_with_nesting_and_anchors(self):
        toc = ncx(point("One", "one.xhtml"), point("Two", "two.xhtml", point("Middle", "two.xhtml#mid")), point("Three", "three.xhtml"))
        self.assertEqual(self.entries(self.book(self.CHAPTERS, ncx=toc)),
                         [("One", 0, 0), ("Two", 3, 0), ("Middle", 5, 1), ("Three", 7, 0)])

    def test_nav_document(self):
        nav = ('<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="landmarks"><ol><li><a href="three.xhtml">Skip</a></li></ol></nav>'
               '<nav epub:type="toc"><ol><li><a href="one.xhtml">One</a></li><li><a href="two.xhtml">T<b>wo</b></a><ol><li><a href="two.xhtml#mid">Middle</a></li></ol></li></ol></nav></body></html>')
        self.assertEqual(self.entries(self.book(self.CHAPTERS, nav=nav)), [("One", 0, 0), ("Two", 3, 0), ("Middle", 5, 1)])

    def test_nav_wins_over_ncx(self):
        nav = '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="one.xhtml">Nav</a></li><li><a href="two.xhtml">Nav2</a></li></ol></nav></body></html>'
        book = self.book(self.CHAPTERS, nav=nav, ncx=ncx(point("Ncx", "one.xhtml"), point("Ncx2", "two.xhtml")))
        self.assertEqual(book.chapters[0].title, "Nav")

    def test_a_broken_nav_falls_back_to_ncx(self):
        book = self.book(self.CHAPTERS, nav="<html><body><p>no list</p></body></html>", ncx=ncx(point("Ncx", "one.xhtml"), point("Ncx2", "two.xhtml")))
        self.assertEqual(book.chapters[0].title, "Ncx")

    def test_links_are_resolved_from_where_the_list_is(self):
        chapters = [("text/ch%201.xhtml", "<p>a</p>"), ("text/two.xhtml", "<p>b</p>")]
        toc = ncx(point("One", "text/ch%201.xhtml"), point("Two", "text/two.xhtml"))
        with self.subTest("encoded name, package in a folder"):
            self.assertEqual(self.entries(self.book(chapters, ncx=toc)), [("One", 0, 0), ("Two", 1, 0)])
        with self.subTest("package at the root of the zip"):
            self.assertEqual(self.entries(self.book(chapters, ncx=toc, opf_dir="")), [("One", 0, 0), ("Two", 1, 0)])

    def test_links_to_files_outside_the_reading_order_are_dropped(self):
        toc = ncx(point("One", "one.xhtml"), point("Gone", "missing.xhtml"), point("Two", "two.xhtml"))
        self.assertEqual([c.title for c in self.book(self.CHAPTERS, ncx=toc).chapters], ["One", "Two"])

    def test_entries_out_of_order_are_sorted_by_position(self):
        toc = ncx(point("Two", "two.xhtml"), point("One", "one.xhtml"))
        self.assertEqual([c.title for c in self.book(self.CHAPTERS, ncx=toc).chapters], ["One", "Two"])

    def test_without_a_list_headings_are_used(self):
        book = self.book([("a.xhtml", "<p>intro</p><h1>First</h1><p>x y</p><h3>Minor</h3><h2>Second</h2><p>z</p>")])
        self.assertEqual(self.entries(book), [("First", 1, 0), ("Second", 5, 0)])

    def test_a_one_entry_list_is_not_treated_as_a_contents(self):
        book = self.book([("a.xhtml", "<h1>First</h1><p>x</p><h1>Second</h1>")], ncx=ncx(point("Whole book", "a.xhtml")))
        self.assertEqual([c.title for c in book.chapters], ["First", "Second"])

    def test_with_no_headings_a_short_bold_line_opening_each_file_is_used(self):
        book = self.book([("a.xhtml", "<p><b>I</b></p><p>x y</p>"), ("b.xhtml", "<p><strong>II</strong></p><p>z</p>")])
        self.assertEqual(self.entries(book), [("I", 0, 0), ("II", 3, 0)])

    def test_bold_lines_inside_a_chapter_are_not_chapters(self):
        # a summary under the chapter number, or a stat block in the text
        book = self.book([("a.xhtml", "<p><b>I</b></p><p><b>Summary</b></p><p>x</p>"), ("b.xhtml", "<p><b>II</b></p><p>y</p><p><b>Status</b></p>")])
        self.assertEqual([c.title for c in book.chapters], ["I", "II"])

    def test_bold_lines_are_ignored_when_real_headings_exist(self):
        book = self.book([("a.xhtml", "<h2>Real</h2>"), ("b.xhtml", "<p><b>Status</b></p>"), ("c.xhtml", "<p><b>Window</b></p>")])
        self.assertEqual([c.title for c in book.chapters], ["Real"])

    def test_part_bold_openings_and_long_bold_passages_are_not_chapters(self):
        long = "<b>" + "word " * 30 + "</b>"
        book = self.book([("a.xhtml", "<p><b>half</b> bold</p>"), ("b.xhtml", f"<p>{long}</p>"), ("c.xhtml", f"<p>{long}</p>")])
        self.assertEqual(book.chapters, [])

    def test_one_bold_opening_is_not_a_contents(self):
        self.assertEqual(self.book([("a.xhtml", "<p><b>Title</b></p>"), ("b.xhtml", "<p>text</p>")]).chapters, [])

    def test_a_book_with_no_list_and_no_headings_has_no_chapters(self):
        self.assertEqual(self.book("<p>just text</p>").chapters, [])

    def test_chapter_lookup_by_position(self):
        book = self.book(self.CHAPTERS, ncx=ncx(point("Two", "two.xhtml"), point("Three", "three.xhtml")))
        self.assertIsNone(book.chapter_at(2))                  # before the first entry
        self.assertEqual(book.chapter_at(3).title, "Two")
        self.assertEqual(book.chapter_at(6).title, "Two")
        self.assertEqual(book.chapter_at(7).title, "Three")
        self.assertEqual(book.chapter_end(4), 7)
        self.assertEqual(book.chapter_end(7), book.words)      # the last chapter runs to the end


class TestUntidyBooks(Base):
    def test_paragraphs_left_open_close_at_the_next(self):
        self.assertEqual(self.texts("<p>a<p>b<p>c"), ["a", "b", "c"])

    def test_list_items_left_open_do_not_staircase(self):
        self.assertEqual([(b.text, b.indent) for b in self.book("<ul><li>a<li>b<li>c</ul>").blocks], [("• a", 2), ("• b", 2), ("• c", 2)])

    def test_a_nested_list_is_not_closed_by_its_own_items(self):
        blocks = self.book("<ul><li>a<ul><li>b<li>c</ul><li>d</ul>").blocks
        self.assertEqual([(b.text, b.indent) for b in blocks], [("• a", 2), ("• b", 4), ("• c", 4), ("• d", 2)])

    def test_table_cells_left_open(self):
        self.assertEqual(self.texts("<table><tr><td>a<td>b<tr><td>c</table>"), ["a · b", "c"])

    def test_head_left_open_closes_at_the_body(self):
        self.assertEqual(self.texts([("a.xhtml", "<html><head><title>x</title><body><p>real</p></body></html>")]), ["real"])

    def test_heading_left_open_closes_at_the_next_paragraph(self):
        self.assertEqual([(b.kind, b.text) for b in self.book("<h1>Head<p>body</p>").blocks], [("h", "Head"), ("p", "body")])

    def test_prefixed_tags_are_understood(self):
        self.assertEqual(self.texts("<xhtml:p>one</xhtml:p><xhtml:p>two</xhtml:p>"), ["one", "two"])

    def test_very_many_open_paragraphs_load_in_good_time(self):
        import time
        started = time.time()
        self.book("".join(f"<p>w{i}" for i in range(20000)))
        self.assertLess(time.time() - started, 5)

    def test_stanza_gaps_survive(self):
        self.assertEqual([(b.text, b.tight) for b in self.book("<p>a<br/>b<br/><br/>c</p>").blocks], [("a", False), ("b", True), ("c", False)])

    def test_blank_lines_in_preformatted_text_survive(self):
        self.assertEqual([(b.text, b.tight) for b in self.book("<pre>a\nb\n\nc</pre>").blocks], [("a", False), ("b", True), ("c", False)])

    def test_carriage_returns_in_preformatted_text(self):
        self.assertEqual(self.texts("<pre>c\rd\r\ne</pre>"), ["c", "d", "e"])

    def test_an_empty_list_item_leaves_no_bullet_behind(self):
        self.assertEqual(self.texts("<ul><li></li></ul><p>Next</p>"), ["Next"])

    def test_numbered_lists_can_start_anywhere(self):
        self.assertEqual(self.texts('<ol start="5"><li>a</li><li>b</li></ol>'), ["5. a", "6. b"])
        self.assertEqual(self.texts('<ol start="x"><li>a</li></ol>'), ["1. a"])

    def test_cdata_is_text(self):
        self.assertIn("text", " ".join(self.texts("<p><![CDATA[text]]></p>")))

    def test_a_scene_break_at_the_end_of_a_file_is_dropped(self):
        book = self.book([("a.xhtml", "<p>a</p><hr/>"), ("b.xhtml", "<p>b</p><hr/><p>c</p>")])
        self.assertEqual([b.text for b in book.blocks], ["a", "b", "* * *", "c"])

    def test_a_line_break_in_a_heading_stays_one_heading(self):
        book = self.book("<h1>Chapter 1<br/>The Beginning</h1><p>x</p>")
        self.assertEqual([c.title for c in book.chapters], ["Chapter 1 The Beginning"])

    def test_small_headings_are_used_when_a_book_has_no_large_ones(self):
        self.assertEqual([c.title for c in self.book("<h3>One</h3><p>x</p><h4>Two</h4>").chapters], ["One", "Two"])

    def test_a_book_of_only_pictures_says_it_has_no_text(self):
        with self.assertRaisesRegex(EbookError, "no text"):
            self.book('<p><img src="a.png"/></p><p><img src="b.png" alt="Map"/></p>')


class TestEncodings(Base):
    def test_utf16_with_a_byte_order_mark(self):
        doc = '<?xml version="1.0" encoding="utf-16"?><html><body><p>café</p></body></html>'.encode("utf-16")
        self.assertEqual(self.texts([("a.xhtml", doc)]), ["café"])

    def test_a_charset_given_only_in_a_meta_tag(self):
        doc = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252"/></head><body><p>“café”</p></body></html>'.encode("cp1252")
        self.assertEqual(self.texts([("a.xhtml", doc)]), ["“café”"])

    def test_a_contents_list_in_an_old_encoding(self):
        toc = '<?xml version="1.0" encoding="gbk"?>' + ncx(point("第一", "a.xhtml"), point("第二", "b.xhtml")).replace('<?xml version="1.0"?>', "")
        book = self.book([("a.xhtml", "<p>x</p>"), ("b.xhtml", "<p>y</p>")], ncx=toc.encode("gbk"))
        self.assertEqual([c.title for c in book.chapters], ["第一", "第二"])

    def test_pictures_listed_in_the_reading_order_are_not_read_as_text(self):
        path = make_book(os.path.join(self.ws.root, "p.epub"), [("a.xhtml", "<p>x</p>")], extra_items=[("cover.jpg", "image/jpeg", b"\xff\xd8junk junk")])
        self.assertEqual([b.text for b in ebook.load(path).blocks], ["x"])


class TestCountWords(Base):
    def count(self, chapters):
        return ebook.count_words(make_book(os.path.join(self.ws.root, "c.epub"), chapters))

    def test_it_agrees_with_opening_the_book(self):
        chapters = [("a.xhtml", "<h1>One two</h1><p>three <em>four</em> five&nbsp;six</p>"), ("b.xhtml", "<p>seven</p><style>.x {color: red}</style>")]
        self.assertEqual(self.count(chapters), 7)
        self.assertEqual(self.count(chapters), ebook.load(os.path.join(self.ws.root, "c.epub")).words)

    def test_the_page_title_and_code_are_not_counted(self):
        doc = "<html><head><title>Not this</title><style>p {color: red}</style></head><body><script>var a = 1;</script><p>real</p></body></html>"
        self.assertEqual(self.count([("a.xhtml", doc)]), 1)

    def test_files_outside_the_reading_order_are_not_counted(self):
        self.assertEqual(self.count([("a.xhtml", "<p>in</p>")]), 1)
        path = make_book(os.path.join(self.ws.root, "d.epub"), [("a.xhtml", "<p>in</p>"), ("b.xhtml", "<p>out</p>")], unlisted=("b.xhtml",))
        self.assertEqual(ebook.count_words(path), 1)

    def test_a_damaged_book_has_no_count(self):
        path = write_file(os.path.join(self.ws.root, "x.epub"), b"nope")
        self.assertIsNone(ebook.count_words(path))


class TestBadFiles(Base):
    def test_not_a_zip(self):
        path = write_file(os.path.join(self.ws.root, "x.epub"), b"not a zip")
        with self.assertRaises(EbookError):
            ebook.load(path)

    def test_missing_file(self):
        with self.assertRaises(EbookError):
            ebook.load(os.path.join(self.ws.root, "nothing.epub"))

    def test_drm_is_named_instead_of_showing_garbage(self):
        with self.assertRaisesRegex(EbookError, "DRM"):
            self.book("<p>x</p>", encrypted=(("a.xhtml", ADOBE_DRM),))

    def test_obfuscated_fonts_are_not_drm(self):
        self.assertEqual(self.texts("<p>x</p>", encrypted=(("font.otf", FONT_OBFUSCATION),)), ["x"])

    def test_a_book_with_no_text_says_so(self):
        with self.assertRaisesRegex(EbookError, "no text"):
            self.book("<p> </p>")

    def test_a_file_missing_from_the_zip_only_loses_that_part(self):
        whole = make_book(os.path.join(self.ws.root, "whole.epub"), [("a.xhtml", "<p>kept</p>"), ("b.xhtml", "<p>lost</p>")])
        cut = os.path.join(self.ws.root, "cut.epub")
        with zipfile.ZipFile(whole) as src, zipfile.ZipFile(cut, "w") as dst:
            for name in src.namelist():
                if name != "OEBPS/b.xhtml":
                    dst.writestr(name, src.read(name))
        self.assertEqual([b.text for b in ebook.load(cut).blocks], ["kept"])

    def test_a_contents_link_closed_in_the_wrong_order_does_not_stop_the_book(self):
        nav = '<html xmlns:epub="x"><body><nav epub:type="toc"><ol><li><a href="a.xhtml">A</nav></a></body></html>'
        self.assertEqual(self.texts("<p>kept</p>", nav=nav), ["kept"])

    def test_any_failure_inside_a_book_is_reported_not_raised(self):
        with mock.patch.object(ebook, "_contents", side_effect=lzma.LZMAError("bad")):
            with self.assertRaises(EbookError):
                self.book("<p>x</p>")

    def test_a_chapter_too_large_to_unpack_is_refused(self):
        with mock.patch.object(ebook, "MAX_DOCUMENT", 10):
            with self.assertRaisesRegex(EbookError, "too large"):
                self.book("<p>more than ten bytes</p>")

    def test_a_book_too_large_in_total_is_refused(self):
        with mock.patch.object(ebook, "MAX_BOOK", 10):
            with self.assertRaisesRegex(EbookError, "too large"):
                self.book("<p>more than ten bytes</p>")

    def test_print_only_rules_do_not_hide_text(self):
        self.assertEqual(self.texts('<p class="x">shown</p>', css="@media print { .x { display: none } }"), ["shown"])


if __name__ == "__main__":
    unittest.main()
