"""Finding books in a folder, naming them, and recognising a book after it has moved or been edited."""

from __future__ import annotations

import os
import shutil
import unittest
import zipfile

from bookread import library
from bookread.library import book_key, is_readable, matches, read_entry, scan
from tests.bookread.helpers import Workspace, make_book, write_file

CHAPTERS = [("a.xhtml", "<p>the text of the book</p>")]
SIDECAR = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Sidecar Title</dc:title><dc:creator>Side Author</dc:creator></metadata></package>')
SERIES = '<meta name="calibre:series" content="Saga"/><meta name="calibre:series_index" content="2"/>'
SERIES3 = ('<meta property="belongs-to-collection" id="c">Saga</meta><meta refines="#c" property="group-position">3</meta>')


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)

    def book(self, rel, **kw) -> str:
        return make_book(os.path.join(self.ws.books, rel), kw.pop("chapters", CHAPTERS), **kw)


class TestScan(Base):
    def test_books_are_found_at_any_depth_sorted_by_title(self):
        self.book("A/B/Zed.epub", title="Zed")
        self.book("Alpha.epub", title="alpha")
        self.assertEqual([e.title for e in scan(self.ws.books)], ["alpha", "Zed"])

    def test_a_series_stays_together_in_reading_order(self):
        self.book("1.epub", title="Zebra Book", extra_meta='<meta name="calibre:series" content="Saga"/><meta name="calibre:series_index" content="10"/>')
        self.book("2.epub", title="Alpha Book", extra_meta='<meta name="calibre:series" content="Saga"/><meta name="calibre:series_index" content="2"/>')
        self.book("3.epub", title="Middle")
        self.book("4.epub", title="Zoo")
        self.assertEqual([e.title for e in scan(self.ws.books)], ["Middle", "Alpha Book", "Zebra Book", "Zoo"])

    def test_numbers_in_titles_sort_as_numbers(self):
        for n in (10, 2, 1):
            self.book(f"{n}.epub", title=f"Vol {n}")
        self.assertEqual([e.title for e in scan(self.ws.books)], ["Vol 1", "Vol 2", "Vol 10"])

    def test_other_files_and_hidden_folders_are_skipped(self):
        self.book("Real.epub", title="Real")
        write_file(f"{self.ws.books}/cover.jpg")
        write_file(f"{self.ws.books}/x.epub.part")
        self.book(".trash/Gone.epub", title="Gone")
        self.assertEqual([e.title for e in scan(self.ws.books)], ["Real"])

    def test_a_missing_folder_is_an_empty_list(self):
        self.assertEqual(scan(f"{self.ws.root}/nowhere"), [])

    def test_a_damaged_book_is_still_listed_so_opening_can_explain(self):
        write_file(f"{self.ws.books}/Broken - Ann Lee.epub", b"not a zip")
        entry = scan(self.ws.books)[0]
        self.assertEqual((entry.title, entry.authors), ("Broken", ["Ann Lee"]))


class TestDamageIsContained(Base):
    """A single bad file must never take the menu down with it."""

    def good_and(self, name: str, build) -> list[str]:
        self.book("Good.epub", title="Good")
        build(os.path.join(self.ws.books, name))
        return [e.title for e in scan(self.ws.books)]

    def package_with(self, declaration: bytes, title: bytes):
        def build(path):
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
                z.writestr("c.opf", declaration + b'<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>' + title + b"</dc:title></metadata></package>")
        return build

    def test_a_corrupt_compressed_package(self):
        def build(path):
            make_book(path, CHAPTERS)
            with zipfile.ZipFile(path) as z:
                names = {i.filename: i for i in z.infolist()}
            info = names["OEBPS/content.opf"]
            # rewrite compressed so the stream can be damaged
            temp = path + ".t"
            with zipfile.ZipFile(path) as old, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as new:
                for i in old.infolist():
                    new.writestr(i.filename, old.read(i) * 20)
            data = bytearray(open(temp, "rb").read())
            with zipfile.ZipFile(temp) as z:
                info = z.getinfo("OEBPS/content.opf")
            start = info.header_offset + 30 + len(info.filename) + 5
            for i in range(start, start + 20):
                data[i] ^= 0xff
            open(path, "wb").write(bytes(data))
            os.remove(temp)
        self.assertEqual(self.good_and("Bad - Ann Lee.epub", build), ["Bad", "Good"])

    def test_old_chinese_and_japanese_encodings_are_read(self):
        declaration = '<?xml version="1.0" encoding="gb2312"?>'.encode()
        self.assertEqual(self.good_and("x.epub", self.package_with(declaration, "红楼梦".encode("gb2312"))), ["Good", "红楼梦"])

    def test_an_unknown_encoding_is_read_as_utf8(self):
        declaration = b'<?xml version="1.0" encoding="x-foo"?>'
        self.assertIn("Title", self.good_and("Bad.epub", self.package_with(declaration, b"Title")))

    def test_a_title_with_a_superscript_can_be_sorted(self):
        self.assertEqual(self.good_and("x.epub", lambda p: make_book(p, CHAPTERS, title="Area 51²")), ["Area 51²", "Good"])

    def test_control_characters_never_reach_a_title(self):
        path = write_file(f"{self.ws.books}/\x1b]0;PWNED\x07 Book - A\x1b[31m.mobi")
        entry = read_entry(path)
        self.assertNotIn("\x1b", entry.title + "".join(entry.authors))
        self.assertNotIn("\x07", entry.title)


class TestNames(Base):
    def test_title_and_authors_come_from_inside_the_epub(self):
        e = read_entry(self.book("x.epub", title="Inside", authors=("Ann Lee", "Bob Ray")))
        self.assertEqual((e.title, e.authors), ("Inside", ["Ann Lee", "Bob Ray"]))

    def test_a_metadata_file_beside_the_book_wins(self):
        path = self.book("A/x.epub", title="Inside", authors=("Ann Lee",))
        write_file(f"{self.ws.books}/A/metadata.opf", SIDECAR)
        e = read_entry(path)
        self.assertEqual((e.title, e.authors), ("Sidecar Title", ["Side Author"]))

    def test_a_metadata_file_is_ignored_when_two_books_share_the_folder(self):
        path = self.book("A/x.epub", title="Inside")
        self.book("A/y.epub", title="Other")
        write_file(f"{self.ws.books}/A/metadata.opf", SIDECAR)
        self.assertEqual(read_entry(path).title, "Inside")

    def test_a_damaged_metadata_file_falls_back_to_the_book(self):
        path = self.book("A/x.epub", title="Inside")
        write_file(f"{self.ws.books}/A/metadata.opf", "<not xml")
        self.assertEqual(read_entry(path).title, "Inside")

    def test_file_names_are_the_last_resort(self):
        table = {"Dune - Frank Herbert.mobi": ("Dune", ["Frank Herbert"]), "dune.azw3": ("dune", []),
                 "A - B - Cee.fb2": ("A - B", ["Cee"])}
        for name, want in table.items():
            with self.subTest(name):
                e = read_entry(write_file(f"{self.ws.books}/{name}"))
                self.assertEqual((e.title, e.authors), want)

    def test_series(self):
        table = [(SERIES, ("Saga", 2.0)), (SERIES3, ("Saga", 3.0)), ("", ("", None))]
        for meta, want in table:
            with self.subTest(meta):
                e = read_entry(self.book("s.epub", extra_meta=meta))
                self.assertEqual((e.series, e.series_index), want)


class TestReadable(unittest.TestCase):
    def test_formats(self):
        table = {"a.epub": True, "A.EPUB": True, "a.kepub": True, "a.mobi": False, "a.pdf": False, "a.azw3": False}
        for name, want in table.items():
            with self.subTest(name):
                self.assertEqual(is_readable(name), want)


class TestKey(Base):
    def rewrite_package(self, path: str) -> None:
        """What a metadata editor does: the package document changes and every other entry is copied as it was."""
        temp = path + ".tmp"
        with zipfile.ZipFile(path) as old, zipfile.ZipFile(temp, "w") as new:
            for info in old.infolist():
                data = old.read(info)
                new.writestr(info, data.replace(b"Test", b"Edited") if info.filename.endswith(".opf") else data)
        os.replace(temp, path)

    def test_a_moved_book_keeps_its_key(self):
        path = self.book("A/x.epub")
        key = book_key(path)
        moved = f"{self.ws.books}/B/renamed.epub"
        os.makedirs(os.path.dirname(moved))
        shutil.move(path, moved)
        self.assertEqual(book_key(moved), key)

    def test_editing_the_metadata_inside_keeps_the_key(self):
        path = self.book("x.epub")
        key = book_key(path)
        self.rewrite_package(path)
        self.assertEqual(book_key(path), key)
        self.assertEqual(read_entry(path).title, "Edited")

    def test_different_text_is_a_different_book(self):
        a = self.book("a.epub", chapters=[("a.xhtml", "<p>one</p>")])
        b = self.book("b.epub", chapters=[("a.xhtml", "<p>two</p>")])
        self.assertNotEqual(book_key(a), book_key(b))

    def test_a_damaged_book_is_keyed_by_its_path(self):
        path = write_file(f"{self.ws.books}/bad.epub", b"nope")
        self.assertEqual(book_key(path), path)


class TestMatches(unittest.TestCase):
    def test_every_word_must_appear_in_title_author_or_series(self):
        entry = library.Entry("/b/x.epub", "Long Road", ["Ann Lee"], "Saga")
        table = {"": True, "long": True, "ROAD ann": True, "saga lee": True, "long pier": False}
        for query, want in table.items():
            with self.subTest(query):
                self.assertEqual(matches(entry, query), want)


if __name__ == "__main__":
    unittest.main()
