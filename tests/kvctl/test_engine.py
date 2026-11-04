"""Imports, plans, deletion rules, author merging, editing and undo, on temp folders."""

from __future__ import annotations

import os
import shutil
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

from kvctl import calibre, engine, epub, metadata
from kvctl.calibre import BookInfo, read_opf
from kvctl.db import DB
from kvctl.metadata import read_book
from kvctl.providers import BookMatch, Lookup, title_variants
from tests.kvctl.helpers import JPEG, FakeTransmission, Workspace, make_epub, torrent_dict, write_file


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.cfg = self.ws.cfg
        self.db = DB(self.cfg["database"])
        self.addCleanup(self.db.close)

    def folder_source(self, name: str) -> engine.Source:
        sources, _ = engine.list_sources(self.cfg)
        return next(s for s in sources if s.name == name)

    def plan(self, source: engine.Source) -> engine.Plan:
        plan = engine.make_plan(source, self.cfg)
        engine.check_plan(plan, self.cfg, self.db)
        return plan

    def apply(self, plan, **kw) -> engine.Result:
        return engine.apply_plan(plan, self.cfg, self.db, **kw)

    def torrent_source(self, name: str, tid: int = 7) -> tuple[engine.Source, FakeTransmission]:
        tr = FakeTransmission([torrent_dict(tid, name, self.ws.downloads)])
        sources, error = engine.list_sources(self.cfg, tr)
        self.assertEqual(error, "")
        return next(s for s in sources if s.kind == "torrent"), tr


def damage_member(path: str, member: str) -> None:
    """Recompress the zip and scramble one member's deflate stream, as a bad download or disk would."""
    with zipfile.ZipFile(path) as old:
        items = [(i.filename, old.read(i)) for i in old.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as new:
        for name, data in items:
            new.writestr(name, data * 50 if name == member else data)
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(member)
    with open(path, "r+b") as f:
        f.seek(info.header_offset + 30 + len(info.filename))
        f.write(b"\xff" * min(20, info.compress_size))


class TestNothingUnknownIsDeleted(Base):
    """Finding 1: only files known to be junk may go."""

    def test_file_types_the_importer_has_never_heard_of_stop_the_cleanup(self):
        for name in ("audiobook.m4b", "show.webm", "comic.cbz", "extras.zip", "track.wma", "video.ts", "NOEXTENSION", "data.xyz"):
            with self.subTest(name):
                folder = f"{self.ws.downloads}/{name}-pack"
                make_epub(f"{folder}/book.epub", f"Book {name}")
                write_file(f"{folder}/{name}", b"valuable")
                write_file(f"{folder}/site.url")
                tr = FakeTransmission([torrent_dict(7, f"{name}-pack", self.ws.downloads)])
                sources, _ = engine.list_sources(self.cfg, tr)
                source = next(s for s in sources if s.kind == "torrent")
                plan = self.plan(source)
                self.assertEqual(engine.other_media(plan), [name])
                self.assertEqual(engine.files_to_delete(plan, self.cfg), [])
                self.assertFalse(engine.will_remove_torrent(plan, self.cfg))
                self.apply(plan, transmission=tr)
                self.assertTrue(os.path.exists(f"{folder}/{name}"))

    def test_known_junk_is_still_cleaned_up(self):
        for name in ("info.nfo", "site.url", "readme.txt", "Cover.JPG", "page.html", "x.sfv", "x.md5", "a.torrent", "Thumbs.db", "desktop.ini", ".DS_Store"):
            write_file(f"{self.ws.inbox}/Pack/{name}")
        make_epub(f"{self.ws.inbox}/Pack/book.epub", "Junk Book")
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual(engine.other_media(plan), [])
        self.assertEqual(len(engine.files_to_delete(plan, self.cfg)), 11)


class TestSkippedDuplicates(Base):
    """Finding 2: 'already in the library' only counts if the library has that format."""

    def owned_as_pdf(self, title="Dune"):
        folder = self.ws.add_library_book(title, "Seth Ring", 5)
        os.rename(f"{folder}/{title} - Seth Ring.epub", f"{folder}/{title} - Seth Ring.pdf")

    def test_a_format_the_library_lacks_is_kept(self):
        self.owned_as_pdf()
        make_epub(f"{self.ws.inbox}/Pack/Dune.epub", "Dune")
        make_epub(f"{self.ws.inbox}/Pack/New.epub", "New")
        plan = self.plan(self.folder_source("Pack"))
        dune = next(b for b in plan.books if b.info.title == "Dune")
        self.assertTrue(dune.skip)
        self.assertEqual(dune.new_formats, [".epub"])
        self.assertIn("epub", dune.warning)
        self.assertEqual(engine.files_to_delete(plan, self.cfg), [])
        self.apply(plan)
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/Pack/Dune.epub"))

    def test_a_torrent_holding_a_new_format_is_kept(self):
        self.owned_as_pdf()
        make_epub(f"{self.ws.downloads}/T/Dune.epub", "Dune")
        make_epub(f"{self.ws.downloads}/T/New.epub", "New")
        source, tr = self.torrent_source("T")
        plan = self.plan(source)
        self.assertFalse(engine.will_remove_torrent(plan, self.cfg))
        self.assertFalse(self.apply(plan, transmission=tr).torrent_removed)

    def test_a_format_already_owned_still_goes(self):
        self.ws.add_library_book("Dune", "Seth Ring", 5)
        make_epub(f"{self.ws.inbox}/Pack/Dune.epub", "Dune")
        make_epub(f"{self.ws.inbox}/Pack/New.epub", "New")
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual(engine.files_to_delete(plan, self.cfg), ["Dune.epub"])

    def test_a_book_you_skip_that_is_not_in_the_library_still_goes(self):
        make_epub(f"{self.ws.inbox}/Pack/Skip.epub", "Skip")
        make_epub(f"{self.ws.inbox}/Pack/New.epub", "New")
        plan = self.plan(self.folder_source("Pack"))
        next(b for b in plan.books if b.info.title == "Skip").skip = True
        engine.check_plan(plan, self.cfg, self.db)
        self.assertEqual(engine.files_to_delete(plan, self.cfg), ["Skip.epub"])

    def test_importing_the_copy_after_all_clears_the_note(self):
        self.owned_as_pdf()
        make_epub(f"{self.ws.inbox}/Pack/Dune.epub", "Dune")
        plan = self.plan(self.folder_source("Pack"))
        plan.books[0].skip = False
        engine.check_plan(plan, self.cfg, self.db)
        self.assertEqual(plan.books[0].new_formats, [])


class TestDamagedBooks(Base):
    """Finding 3: a corrupt zip stream must stay one book's problem."""

    OPF = "OEBPS/content.opf"
    CHAPTER = "OEBPS/text.xhtml"

    def test_reading_metadata_falls_back_to_the_file_name(self):
        path = make_epub(f"{self.ws.root}/Dune - Ann Lee.epub", "Dune")
        damage_member(path, self.OPF)
        info, _, _, source = read_book(path)
        self.assertEqual(info.title, "Dune")
        self.assertIn("unreadable", source)

    def test_reading_authors_is_a_book_error(self):
        path = make_epub(f"{self.ws.root}/x.epub", "X")
        damage_member(path, self.OPF)
        with self.assertRaises(epub.EpubError):
            epub.read_authors(path)

    def test_rewriting_a_book_with_a_bad_chapter_leaves_it_alone(self):
        path = make_epub(f"{self.ws.root}/x.epub", "X")
        damage_member(path, self.CHAPTER)
        with open(path, "rb") as f:
            before = f.read()
        with self.assertRaises(epub.EpubError):
            epub.write_metadata(path, authors=["New Name"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(self.ws.root).count("x.epub.kvctl-tmp"), 0)

    def test_importing_it_finishes_and_says_so(self):
        path = make_epub(f"{self.ws.downloads}/T/Bad.epub", "Bad Book", ["Ring, Seth"])
        damage_member(path, self.CHAPTER)
        write_file(f"{self.ws.downloads}/T/site.url")
        source, tr = self.torrent_source("T")
        res = self.apply(self.plan(source), transmission=tr)
        self.assertEqual(res.imported, 1)
        self.assertTrue(any("not changed" in e for e in res.errors), res.errors)
        self.assertEqual(self.db.operation(res.op_id)["status"], "applied")
        self.assertTrue(res.torrent_removed)

    def test_a_damaged_library_book_does_not_stop_other_imports(self):
        folder = self.ws.add_library_book("Old Bad", "Ann Lee", 5)
        damage_member(f"{folder}/Old Bad - Ann Lee.epub", self.OPF)
        make_epub(f"{self.ws.inbox}/Pack/New.epub", "New")
        res = self.apply(self.plan(self.folder_source("Pack")))
        self.assertEqual((res.imported, res.errors), (1, []))
        self.assertEqual(self.db.operation(res.op_id)["status"], "applied")


class TestHoldingFolder(Base):
    def pack(self):
        make_epub(f"{self.ws.inbox}/Pack/sub/keep.epub", "Keep")
        make_epub(f"{self.ws.inbox}/Pack/sub/skip.epub", "Skip")
        write_file(f"{self.ws.inbox}/Pack/sub/info.nfo", b"notes")
        plan = self.plan(self.folder_source("Pack"))
        next(b for b in plan.books if b.info.title == "Skip").skip = True
        engine.check_plan(plan, self.cfg, self.db)
        return plan

    def test_cleared_files_move_to_the_holding_folder(self):
        res = self.apply(self.pack())
        held = f"{self.cfg['holding_dir']}/{res.op_id}"
        self.assertEqual(self.ws.tree(held), ["sub/info.nfo", "sub/skip.epub"])
        self.assertFalse(os.path.exists(f"{self.ws.inbox}/Pack"))
        self.assertEqual(res.errors, [])
        self.assertEqual(res.held, 2)

    def test_undo_puts_them_back(self):
        res = self.apply(self.pack())
        done, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/Pack"), ["sub/info.nfo", "sub/keep.epub", "sub/skip.epub"])
        with open(f"{self.ws.inbox}/Pack/sub/info.nfo", "rb") as f:
            self.assertEqual(f.read(), b"notes")

    def test_undo_after_the_holding_copy_expired_restores_the_rest(self):
        res = self.apply(self.pack())
        shutil.rmtree(f"{self.cfg['holding_dir']}/{res.op_id}")
        _, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/Pack"), ["sub/keep.epub"])
        self.assertEqual(self.db.operation(res.op_id)["status"], "undone")
        self.assertIn("no longer held", self.db.operation(res.op_id)["message"])

    def test_undo_refuses_if_a_held_files_old_place_is_taken(self):
        res = self.apply(self.pack())
        write_file(f"{self.ws.inbox}/Pack/sub/info.nfo", b"new")
        with self.assertRaises(engine.PlanError):
            engine.undo_operation(res.op_id, self.cfg, self.db)

    def test_if_a_file_cannot_be_held_it_stays_where_it_was(self):
        write_file(self.cfg["holding_dir"], b"a file, so no folder can go here")
        res = self.apply(self.pack())
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/Pack/sub/info.nfo"))
        self.assertTrue(any("info.nfo" in e for e in res.errors), res.errors)

    def test_nothing_is_held_when_leftovers_are_kept(self):
        res = self.apply(self.pack(), delete_leftovers=False)
        self.assertEqual(res.held, 0)
        self.assertFalse(os.path.exists(self.cfg["holding_dir"]))

    def test_old_operations_expire_and_nothing_else_is_touched(self):
        hold = self.cfg["holding_dir"]
        old, recent, other = f"{hold}/3", f"{hold}/4", f"{hold}/my-notes"
        for folder in (old, recent, other):
            write_file(f"{folder}/x")
        ancient = time.time() - 31 * 86400
        for folder in (old, other):
            os.utime(folder, (ancient, ancient))
        engine.purge_held(self.cfg)
        self.assertEqual(sorted(os.listdir(hold)), ["4", "my-notes"])

    def test_importing_clears_expired_operations(self):
        hold = self.cfg["holding_dir"]
        write_file(f"{hold}/3/x")
        os.utime(f"{hold}/3", (1, 1))
        self.apply(self.pack())
        self.assertFalse(os.path.exists(f"{hold}/3"))

    def test_purging_with_no_holding_folder_is_fine(self):
        engine.purge_held(self.cfg)


class TestBooksWithNoId(Base):
    """Finding 4: folders with no id all read as id 0, and must not share one series assignment."""

    def no_id_book(self, title, author, folder):
        path = f"{self.ws.books}/{author}/{folder}"
        make_epub(f"{path}/{title} - {author}.epub", title, [author])
        return path

    def test_series_grouping_keeps_them_apart(self):
        one = self.no_id_book("Foo Book 1", "Ann Lee", "Foo Book 1")
        two = self.no_id_book("Foo Book 2", "Ann Lee", "Foo Book 2")
        self.no_id_book("Unrelated", "Bob Ray", "Unrelated")
        fixes = {os.path.basename(f.book.path): (f.series, f.index) for f in engine.series_fix_plan(self.cfg)}
        self.assertEqual(fixes, {"Foo Book 1": ("Foo", 1.0), "Foo Book 2": ("Foo", 2.0)})
        self.assertTrue(one and two)

    def test_an_import_only_brings_along_books_of_its_series(self):
        self.no_id_book("Deep Dive 101", "Ann Lee", "Deep Dive 101")
        self.no_id_book("Unrelated", "Bob Ray", "Unrelated")
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "Deep Dive 102", ["Ann Lee"])
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual([f.book.info.title for f in plan.series_partners], ["Deep Dive 101"])


class TestUntrustedNames(Base):
    """Finding 9: control characters in names would reach the terminal and make metadata.opf unreadable."""

    def test_metadata_values_are_cleaned(self):
        # EPUB can't hold these, being XML, but a MOBI's title and author are raw bytes
        self.assertEqual(metadata._clean(" a\x1b[31m  b\x07 "), "a[31m b")

    def test_they_never_reach_the_metadata(self):
        info = read_book(make_epub(f"{self.ws.root}/Evil\x1b[31m\x07 Book - Ann\x1b Lee.epub", ""))[0]
        self.assertNotIn("\x1b", info.title + "".join(info.authors))
        self.assertNotIn("\x07", info.title)
        ET.fromstring(calibre.write_opf(info, 1))      # still valid XML


class TestRolesInsideEpub(Base):
    """Finding 5: an EPUB3 translator isn't an author."""

    def book(self, role_meta):
        path = f"{self.ws.inbox}/Pack/a.epub"
        make_epub(path, "Roles", ["Alice Author"])
        with zipfile.ZipFile(path) as z:
            items = {n: z.read(n) for n in z.namelist()}
        opf = items["OEBPS/content.opf"].decode()
        opf = opf.replace("<dc:language>", f'<dc:creator id="c2">Tom Translator</dc:creator>{role_meta}<dc:language>')
        items["OEBPS/content.opf"] = opf.encode()
        with zipfile.ZipFile(path, "w") as z:
            for name, data in items.items():
                z.writestr(name, data)
        return path

    def test_a_translator_declared_by_a_refinement_is_not_read_as_an_author(self):
        path = self.book('<meta refines="#c2" property="role">trl</meta>')
        self.assertEqual(read_book(path)[0].authors, ["Alice Author"])

    def test_a_refinement_saying_author_counts(self):
        path = self.book('<meta refines="#c2" property="role">aut</meta>')
        self.assertEqual(read_book(path)[0].authors, ["Alice Author", "Tom Translator"])

    def test_importing_does_not_promote_the_translator(self):
        self.book('<meta refines="#c2" property="role">trl</meta>')
        self.apply(self.plan(self.folder_source("Pack")))
        moved = next(f for f in self.ws.tree(self.ws.books) if f.endswith(".epub"))
        self.assertEqual(epub.read_authors(os.path.join(self.ws.books, moved)), ["Alice Author"])


class TestClearingASeries(Base):
    """Finding 6."""

    def moved(self):
        return os.path.join(self.ws.books, next(f for f in self.ws.tree(self.ws.books) if f.endswith(".epub")))

    def test_a_series_cleared_in_the_review_leaves_the_book(self):
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "Book", series="Wrong Series", index=2)
        plan = self.plan(self.folder_source("Pack"))
        plan.books[0].info.series, plan.books[0].info.series_index = "", None
        self.apply(plan)
        self.assertEqual(epub.read_series(self.moved()), ("", None))

    def test_a_series_nobody_cleared_stays_even_if_a_sidecar_omits_it(self):
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "Book", series="Keep Me", index=2)
        write_file(f"{self.ws.inbox}/Pack/metadata.opf", calibre.write_opf(BookInfo(title="Book", authors=["Seth Ring"]), 1))
        self.apply(self.plan(self.folder_source("Pack")))
        self.assertEqual(epub.read_series(self.moved()), ("Keep Me", 2.0))


class TestCalibreRules(unittest.TestCase):
    def test_paths_match_calibre(self):
        self.assertEqual(calibre.book_path("Germination: Battle Mage Farmer", "Ring, Seth", 29),
                         "Ring, Seth/Germination_ Battle Mage Farmer (29)")
        self.assertEqual(calibre.file_stem("Germination: Battle Mage Farmer", "Ring, Seth"),
                         "Germination_ Battle Mage Farmer - Ring, Seth")

    def test_truncation_and_reserved_characters(self):
        title = "A Very Long Title That Goes On And On Beyond Every Limit"
        self.assertEqual(calibre.book_folder(title, 3), f"{title[:36].rstrip()} (3)")
        self.assertEqual(calibre.file_stem(title, "Ann Lee"), f"{title[:31].rstrip()} - Ann Lee")
        self.assertEqual(calibre.ascii_filename('a/b:c*d?"e<f>g|'), "a_b_c_d__e_f_g_")
        self.assertEqual(calibre.ascii_filename("Vol."), "Vol_")

    def test_transliteration(self):
        self.assertEqual(calibre.transliterate("Café"), "Cafe")
        self.assertEqual(calibre.transliterate("Сергей"), "Serghei")

    def test_author_helpers(self):
        self.assertEqual(calibre.first_last("Ring, Seth"), "Seth Ring")
        self.assertEqual(calibre.author_sort("William Gibson"), "Gibson, William")
        self.assertEqual(calibre.author_key("Ring, Seth"), calibre.author_key("Seth Ring"))
        self.assertEqual(calibre.author_key("Mathews, J.R_"), calibre.author_key("J.R. Mathews"))
        self.assertEqual(engine.split_authors(["Kugane Maruyama & so-bin", "Ring, Seth"]), ["Kugane Maruyama", "so-bin", "Seth Ring"])
        self.assertEqual(engine.split_authors(["Sam Anderson"]), ["Sam Anderson"])

    def test_opf_round_trip(self):
        info = BookInfo(title="A & B <Title>", authors=["Seth Ring", "Ann O'Neil"], series='The "Saga"', series_index=2.5, language="en",
                        description="Some & text", isbn="9781234567897", publisher="P & Q", pubdate="2020-01-01T00:00:00+00:00",
                        subjects=["LitRPG", "Fantasy"], uuid="abc", timestamp="2026-01-01T00:00:00+00:00", title_sort="Title")
        ws = Workspace()
        self.addCleanup(ws.close)
        path = write_file(os.path.join(ws.root, "metadata.opf"), calibre.write_opf(info, 57))
        back, book_id = read_opf(path)
        self.assertEqual(book_id, 57)
        self.assertEqual(back, info)


class TestMetadata(Base):
    def test_epub(self):
        path = make_epub(f"{self.ws.root}/a.epub", "The Book", ["Seth Ring"], "Saga", 3, JPEG)
        info, cover, ext, source = read_book(path)
        self.assertEqual((info.title, info.authors, info.series, info.series_index), ("The Book", ["Seth Ring"], "Saga", 3.0))
        self.assertEqual((cover, ext, source), (JPEG, ".jpg", "book metadata"))

    def test_damaged_file_falls_back_to_the_name(self):
        path = write_file(f"{self.ws.root}/Some Title - Jo Bloggs.epub", b"not a zip")
        info, cover, _, source = read_book(path)
        self.assertEqual((info.title, info.authors, cover), ("Some Title", ["Jo Bloggs"], None))
        self.assertIn("unreadable", source)

    def test_name_only_formats(self):
        info, _, _, source = read_book(write_file(f"{self.ws.root}/Plain.pdf"))
        self.assertEqual((info.title, info.authors, source), ("Plain", [], "file name"))


class TestPlans(Base):
    def test_lists_only_sources_with_books(self):
        make_epub(f"{self.ws.inbox}/Has Book/a.epub", "A")
        write_file(f"{self.ws.inbox}/No Book/readme.txt")
        write_file(f"{self.ws.inbox}/.hidden/b.epub")
        make_epub(f"{self.ws.downloads}/Torrent Book/t.epub", "T")
        write_file(f"{self.ws.downloads}/Movie/m.mkv")
        tr = FakeTransmission([torrent_dict(1, "Torrent Book", self.ws.downloads), torrent_dict(2, "Movie", self.ws.downloads)])
        sources, _ = engine.list_sources(self.cfg, tr)
        self.assertEqual(sorted((s.kind, s.name) for s in sources), [("folder", "Has Book"), ("torrent", "Torrent Book")])

    def test_unfinished_torrent_is_not_listed(self):
        make_epub(f"{self.ws.downloads}/Partial/a.epub", "A")
        tr = FakeTransmission([torrent_dict(1, "Partial", self.ws.downloads, left=100)])
        self.assertEqual(engine.list_sources(self.cfg, tr)[0], [])

    def test_transmission_failure_keeps_folders(self):
        class Broken:
            def torrent_get(self, fields):
                raise RuntimeError("down")
        make_epub(f"{self.ws.inbox}/Book/a.epub", "A")
        sources, error = engine.list_sources(self.cfg, Broken())
        self.assertEqual([s.name for s in sources], ["Book"])
        self.assertIn("down", error)

    def test_formats_of_one_book_are_grouped(self):
        make_epub(f"{self.ws.inbox}/Pack/The Book - Seth Ring.epub", "The Book", ["Seth Ring"])
        write_file(f"{self.ws.inbox}/Pack/The Book - Seth Ring.pdf")
        make_epub(f"{self.ws.inbox}/Pack/Other - Seth Ring.epub", "Other", ["Seth Ring"])
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual(sorted(len(b.files) for b in plan.books), [1, 2])

    def test_same_title_same_format_stays_separate_and_warns(self):
        make_epub(f"{self.ws.inbox}/Pack/one/a.epub", "Twin", ["Seth Ring"])
        make_epub(f"{self.ws.inbox}/Pack/two/b.epub", "Twin", ["Seth Ring"])
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual(len(plan.books), 2)
        self.assertIn("another book here", plan.books[1].warning)

    def test_co_authors_are_split_and_first_names_the_folder(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Title", ["Kugane Maruyama & so-bin"])
        plan = self.plan(self.folder_source("B"))
        self.assertEqual(plan.books[0].info.authors, ["Kugane Maruyama", "so-bin"])
        self.assertTrue(plan.books[0].dest.endswith("Kugane Maruyama/Title (1)"))

    def test_sidecars_of_a_one_book_folder_are_used(self):
        make_epub(f"{self.ws.inbox}/Export/a.epub", "Wrong Title", ["Nobody"])
        write_file(f"{self.ws.inbox}/Export/cover.jpg", JPEG)
        write_file(f"{self.ws.inbox}/Export/metadata.opf", calibre.write_opf(BookInfo(title="Right Title", authors=["Seth Ring"], series="S"), 4))
        plan = self.plan(self.folder_source("Export"))
        book = plan.books[0]
        self.assertEqual((book.info.title, book.info.series, book.source, book.cover), ("Right Title", "S", "metadata.opf", JPEG))
        self.assertEqual(plan.leftovers, [])

    def test_sidecars_beside_several_books_belong_to_none(self):
        make_epub(f"{self.ws.inbox}/Pack/a.epub", "First", ["Seth Ring"])
        make_epub(f"{self.ws.inbox}/Pack/b.epub", "Second", ["Seth Ring"])
        write_file(f"{self.ws.inbox}/Pack/cover.jpg", JPEG)
        write_file(f"{self.ws.inbox}/Pack/metadata.opf", calibre.write_opf(BookInfo(title="Pack Title", authors=["X"]), 4))
        plan = self.plan(self.folder_source("Pack"))
        self.assertEqual(sorted(b.info.title for b in plan.books), ["First", "Second"])
        self.assertEqual([b.cover for b in plan.books], [None, None])
        self.assertEqual(sorted(r for r, _ in plan.leftovers), ["cover.jpg", "metadata.opf"])

    def test_problems_block_applying(self):
        make_epub(f"{self.ws.inbox}/B/Nameless.epub", "Has Title", ())
        plan = self.plan(self.folder_source("B"))
        self.assertEqual(plan.books[0].error, "no author")
        self.assertFalse(engine.plan_ok(plan))
        with self.assertRaises(engine.PlanError):
            self.apply(plan)
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/B/Nameless.epub"))

    def test_incomplete_files_block_applying(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "A")
        write_file(f"{self.ws.inbox}/B/b.epub.part")
        plan = self.plan(self.folder_source("B"))
        self.assertFalse(engine.plan_ok(plan))

    def test_duplicates_start_skipped_and_follow_edits(self):
        self.ws.add_library_book("Known", "Seth Ring", 5)
        make_epub(f"{self.ws.inbox}/B/a.epub", "Known", ["Ring, Seth"])
        plan = self.plan(self.folder_source("B"))
        book = plan.books[0]
        self.assertTrue(book.skip)
        self.assertEqual(book.duplicate_of, 5)
        book.skip = False
        engine.check_plan(plan, self.cfg, self.db)
        self.assertIn("second copy", book.warning)
        book.info.title = "Different"
        engine.check_plan(plan, self.cfg, self.db)
        self.assertIsNone(book.duplicate_of)


class TestImport(Base):
    def test_layout_metadata_and_id_sequence(self):
        self.ws.add_library_book("Old", "Seth Ring", 56)
        make_epub(f"{self.ws.inbox}/B/x.epub", "Book: One", ["Ring, Seth"], "Saga", 1, JPEG)
        res = self.apply(self.plan(self.folder_source("B")))
        self.assertEqual((res.imported, res.errors), (1, []))
        folder = f"{self.ws.books}/Seth Ring/Book_ One (57)"
        self.assertEqual(sorted(os.listdir(folder)), ["Book_ One - Seth Ring.epub", "cover.jpg", "metadata.opf"])
        info, book_id = read_opf(f"{folder}/metadata.opf")
        self.assertEqual((book_id, info.title, info.authors, info.series, info.series_index), (57, "Book: One", ["Seth Ring"], "Saga", 1.0))
        self.assertTrue(info.uuid and info.timestamp)
        with open(f"{folder}/cover.jpg", "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertFalse(os.path.exists(f"{self.ws.inbox}/B"))     # the emptied source is gone
        self.assertTrue(os.path.isdir(self.ws.inbox))               # the watched folder stays

    def test_ids_are_never_reused(self):
        make_epub(f"{self.ws.inbox}/B/x.epub", "One")
        res = self.apply(self.plan(self.folder_source("B")))
        self.assertTrue(os.path.isdir(f"{self.ws.books}/Seth Ring/One (1)"))
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(engine.next_id(self.cfg, self.db), 2)

    def test_several_books_get_consecutive_ids(self):
        make_epub(f"{self.ws.inbox}/P/a.epub", "Alpha")
        make_epub(f"{self.ws.inbox}/P/b.epub", "Beta")
        self.apply(self.plan(self.folder_source("P")))
        self.assertEqual(sorted(os.listdir(f"{self.ws.books}/Seth Ring")), ["Alpha (1)", "Beta (2)"])

    def test_unnamed_image_type_is_not_written_as_jpeg(self):
        make_epub(f"{self.ws.inbox}/B/x.epub", "Gif Book", cover=b"GIF89a-image")
        self.apply(self.plan(self.folder_source("B")))
        folder = f"{self.ws.books}/Seth Ring/Gif Book (1)"
        self.assertNotIn("cover.jpg", os.listdir(folder))
        self.assertEqual(sorted(os.listdir(folder))[-1], "metadata.opf")

    def test_online_match_only_fills_gaps(self):
        make_epub(f"{self.ws.inbox}/B/x.epub", "Mine", ["Me Myself"])
        plan = self.plan(self.folder_source("B"))
        book = plan.books[0]
        match = BookMatch("Open Library", "Theirs", ["Them"], 2001, "9780000000000", 123, "Pub", "eng", "/w/1")
        engine.apply_match(book, match)
        self.assertEqual((book.info.title, book.info.authors, book.info.isbn, book.info.publisher), ("Mine", ["Me Myself"], "", ""))
        self.assertEqual(book.cover_id, 123)
        self.assertTrue(book.info.pubdate.startswith("2001"))
        self.assertEqual(book.verified, "Open Library")
        engine.apply_match(book, match, overwrite=True)
        self.assertEqual((book.info.title, book.info.authors), ("Theirs", ["Them"]))

    def test_skipped_book_and_leftovers_are_deleted_from_a_watched_folder(self):
        make_epub(f"{self.ws.inbox}/P/a.epub", "Keep")
        make_epub(f"{self.ws.inbox}/P/b.epub", "Skip")
        write_file(f"{self.ws.inbox}/P/info.nfo")
        plan = self.plan(self.folder_source("P"))
        next(b for b in plan.books if b.info.title == "Skip").skip = True
        self.assertEqual(sorted(engine.files_to_delete(plan, self.cfg)), ["b.epub", "info.nfo"])
        res = self.apply(plan)
        self.assertEqual(res.held, 2)
        self.assertFalse(os.path.exists(f"{self.ws.inbox}/P"))

    def test_keep_leftovers_deletes_nothing(self):
        make_epub(f"{self.ws.inbox}/P/a.epub", "Keep")
        write_file(f"{self.ws.inbox}/P/info.nfo")
        self.apply(self.plan(self.folder_source("P")), delete_leftovers=False)
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/P"), ["info.nfo"])

    def test_all_skipped_changes_nothing(self):
        make_epub(f"{self.ws.inbox}/P/a.epub", "Only")
        write_file(f"{self.ws.inbox}/P/info.nfo")
        plan = self.plan(self.folder_source("P"))
        plan.books[0].skip = True
        with self.assertRaises(engine.PlanError):
            self.apply(plan)
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/P"), ["a.epub", "info.nfo"])

    def test_folder_named_by_hand_keeps_everything_else(self):
        other = f"{self.ws.root}/Documents"
        make_epub(f"{other}/sub/a.epub", "Mine")
        write_file(f"{other}/tax-return.pdf.txt")
        write_file(f"{other}/photo.jpg")
        os.makedirs(f"{other}/empty-but-not-ours")
        plan = self.plan(engine.source_for_path(other))
        self.assertEqual(engine.files_to_delete(plan, self.cfg), [])
        res = self.apply(plan)
        self.assertEqual((res.imported, res.held), (1, 0))
        self.assertEqual(self.ws.tree(other), ["photo.jpg", "tax-return.pdf.txt"])
        self.assertTrue(os.path.isdir(f"{other}/empty-but-not-ours"))
        self.assertFalse(os.path.exists(f"{other}/sub"))

    def test_single_file_leaves_its_folder_alone(self):
        path = make_epub(f"{self.ws.root}/Desk/a.epub", "Solo")
        write_file(f"{self.ws.root}/Desk/metadata.opf", calibre.write_opf(BookInfo(title="Someone Else", authors=["X"]), 9))
        write_file(f"{self.ws.root}/Desk/notes.txt")
        plan = self.plan(engine.source_for_path(path))
        self.assertEqual(plan.books[0].info.title, "Solo")
        self.apply(plan)
        self.assertEqual(self.ws.tree(f"{self.ws.root}/Desk"), ["metadata.opf", "notes.txt"])
        self.assertTrue(os.path.isdir(f"{self.ws.books}/Seth Ring/Solo (1)"))

    def test_links_are_ignored(self):
        secret = write_file(f"{self.ws.root}/outside.epub")
        make_epub(f"{self.ws.inbox}/P/a.epub", "Real")
        os.symlink(secret, f"{self.ws.inbox}/P/link.epub")
        plan = self.plan(self.folder_source("P"))
        self.assertEqual([b.info.title for b in plan.books], ["Real"])
        self.assertEqual(plan.leftovers, [])
        self.apply(plan)
        self.assertTrue(os.path.exists(secret))

    def test_torrent_is_stopped_then_removed_keeping_data_flag_off(self):
        make_epub(f"{self.ws.downloads}/T/a.epub", "From Torrent")
        write_file(f"{self.ws.downloads}/T/site.url")
        source, tr = self.torrent_source("T")
        res = self.apply(self.plan(source), transmission=tr)
        self.assertEqual(tr.calls, [("stop", [7]), ("remove", [7], False)])
        self.assertTrue(res.torrent_removed)
        self.assertFalse(os.path.exists(f"{self.ws.downloads}/T"))

    def test_mixed_torrent_keeps_media_and_the_torrent(self):
        make_epub(f"{self.ws.downloads}/Mixed/book.epub", "A Book")
        write_file(f"{self.ws.downloads}/Mixed/film.mkv", b"video")
        write_file(f"{self.ws.downloads}/Mixed/song.flac", b"music")
        write_file(f"{self.ws.downloads}/Mixed/info.nfo")
        source, tr = self.torrent_source("Mixed")
        plan = self.plan(source)
        self.assertEqual(sorted(engine.other_media(plan)), ["film.mkv", "song.flac"])
        self.assertFalse(engine.will_remove_torrent(plan, self.cfg))
        self.assertEqual(engine.files_to_delete(plan, self.cfg), [])
        res = self.apply(plan, transmission=tr)
        self.assertEqual(res.imported, 1)
        self.assertFalse(res.torrent_removed)
        self.assertEqual(tr.calls, [("stop", [7])])
        self.assertEqual(self.ws.tree(f"{self.ws.downloads}/Mixed"), ["film.mkv", "info.nfo", "song.flac"])

    def test_mixed_torrent_can_be_removed_when_asked(self):
        make_epub(f"{self.ws.downloads}/Mixed/book.epub", "A Book")
        write_file(f"{self.ws.downloads}/Mixed/film.mkv", b"video")
        source, tr = self.torrent_source("Mixed")
        res = self.apply(self.plan(source), transmission=tr, remove_torrent=True)
        self.assertTrue(res.torrent_removed)
        self.assertEqual(self.ws.tree(f"{self.ws.downloads}/Mixed"), ["film.mkv"])

    def test_keep_torrent_option(self):
        make_epub(f"{self.ws.downloads}/T/a.epub", "Book")
        source, tr = self.torrent_source("T")
        self.apply(self.plan(source), transmission=tr, remove_torrent=False)
        self.assertEqual(tr.calls, [("stop", [7])])

    def test_failed_move_keeps_the_source_and_restarts_an_untouched_torrent(self):
        make_epub(f"{self.ws.downloads}/T/a.epub", "Clash")
        write_file(f"{self.ws.downloads}/T/info.nfo")
        source, tr = self.torrent_source("T")
        with mock.patch.object(engine, "move_file", side_effect=PermissionError(13, "Permission denied", "a.epub")):
            res = self.apply(self.plan(source), transmission=tr)
        self.assertEqual(res.imported, 0)
        self.assertTrue(res.errors)
        self.assertEqual(tr.calls, [("stop", [7]), ("start", [7])])
        self.assertEqual(self.ws.tree(f"{self.ws.downloads}/T"), ["a.epub", "info.nfo"])
        self.assertEqual(self.db.operation(res.op_id)["status"], "failed")

    def test_kavita_failure_does_not_undo_an_import(self):
        class Broken:
            def library_for_folder(self, folder):
                raise RuntimeError("no connection")
        make_epub(f"{self.ws.inbox}/B/a.epub", "Book")
        res = self.apply(self.plan(self.folder_source("B")), kavita=Broken())
        self.assertEqual(res.imported, 1)
        self.assertIn("scan failed", res.scanned)
        self.assertEqual(self.db.operation(res.op_id)["status"], "applied")


class TestUndo(Base):
    def test_import_undo_restores_files_and_removes_what_it_added(self):
        make_epub(f"{self.ws.inbox}/B/sub/a.epub", "Book", cover=JPEG)
        before = self.ws.tree(f"{self.ws.inbox}/B")
        res = self.apply(self.plan(self.folder_source("B")))
        done, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])
        self.assertEqual(done, 3)     # book, cover, metadata.opf
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/B"), before)
        self.assertEqual(self.ws.tree(self.ws.books), [])
        self.assertEqual(os.listdir(self.ws.books), [])
        self.assertEqual(self.db.operation(res.op_id)["status"], "undone")
        with self.assertRaises(engine.PlanError):
            engine.undo_operation(res.op_id, self.cfg, self.db)

    def test_readd_torrent(self):
        make_epub(f"{self.ws.downloads}/T/a.epub", "Book")
        source, tr = self.torrent_source("T")
        res = self.apply(self.plan(source), transmission=tr)
        engine.undo_operation(res.op_id, self.cfg, self.db, tr, readd_torrent=True)
        self.assertEqual(tr.calls[-1][0], "add")
        self.assertEqual(tr.calls[-1][1]["download_dir"], self.ws.downloads)
        self.assertTrue(os.path.exists(f"{self.ws.downloads}/T/a.epub"))

    def test_torrent_that_was_kept_is_not_added_twice(self):
        make_epub(f"{self.ws.downloads}/T/a.epub", "Book")
        source, tr = self.torrent_source("T")
        res = self.apply(self.plan(source), transmission=tr, remove_torrent=False)
        engine.undo_operation(res.op_id, self.cfg, self.db, tr, readd_torrent=True)
        self.assertNotIn("add", [c[0] for c in tr.calls])

    def test_older_operation_is_refused_until_the_later_one_is_undone(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Book", ["Ring, Seth"])
        imported = self.apply(self.plan(self.folder_source("B")))
        book = engine.library_books(self.cfg)[0]
        info = BookInfo(title="Renamed", authors=["Seth Ring"])
        edited = engine.edit_book(book, info, self.cfg, self.db)
        tree = self.ws.tree()
        with self.assertRaises(engine.PlanError) as caught:
            engine.undo_operation(imported.op_id, self.cfg, self.db)
        self.assertIn("undo that one first", str(caught.exception))
        self.assertEqual(self.ws.tree(), tree)      # refused before touching anything
        engine.undo_operation(edited.op_id, self.cfg, self.db)
        engine.undo_operation(imported.op_id, self.cfg, self.db)
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/B/a.epub"))
        self.assertEqual(os.listdir(self.ws.books), [])

    def test_id_of_a_half_finished_book_is_not_reused(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Book")
        plan = self.plan(self.folder_source("B"))
        write_file(os.path.join(plan.books[0].dest, "stray.txt"))
        res = self.apply(plan)
        self.assertEqual(res.errors, [])
        self.assertTrue(os.path.isdir(f"{self.ws.books}/Seth Ring/Book (2)"))

    def test_failed_import_can_be_undone(self):
        make_epub(f"{self.ws.inbox}/P/a.epub", "Alpha")
        make_epub(f"{self.ws.inbox}/P/b.epub", "Beta")
        real, calls = engine.move_file, []

        def second_fails(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise engine.MoveRefused("refusing to overwrite")
            real(src, dst)
        with mock.patch.object(engine, "move_file", second_fails):
            res = self.apply(self.plan(self.folder_source("P")))
        self.assertEqual((res.imported, len(res.errors)), (1, 1))
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(self.ws.tree(f"{self.ws.inbox}/P"), ["a.epub", "b.epub"])


class TestMergeAndEdit(Base):
    def test_merge_plan_covers_reversed_and_split_authors(self):
        self.ws.add_library_book("One", "Ring, Seth", 1)
        self.ws.add_library_book("Two", "Seth Ring", 2)
        self.ws.add_library_book("Three", "Mathews, J.R.", 3)
        self.ws.add_library_book("Four", "Ann Lee", 4)
        plan = engine.author_merge_plan(self.cfg)
        self.assertEqual(sorted((r.book.id, r.new_author) for r in plan), [(1, "Seth Ring"), (3, "J.R. Mathews")])
        self.assertEqual(sorted(os.path.relpath(r.new_path, self.ws.books) for r in plan),
                         ["J.R. Mathews/Three (3)", "Seth Ring/One (1)"])

    def test_merge_apply_and_undo_are_exact(self):
        old = self.ws.add_library_book("One", "Ring, Seth", 1)
        with open(f"{old}/metadata.opf", encoding="utf-8") as f:
            original = f.read()
        tree = self.ws.tree(self.ws.books)
        res = engine.apply_renames(engine.author_merge_plan(self.cfg), self.cfg, self.db)
        self.assertEqual((res.imported, res.errors), (1, []))
        new = f"{self.ws.books}/Seth Ring/One (1)"
        self.assertEqual(sorted(os.listdir(new)), ["One - Seth Ring.epub", "cover.jpg", "metadata.opf"])
        info, book_id = read_opf(f"{new}/metadata.opf")
        self.assertEqual((info.authors, book_id), (["Seth Ring"], 1))
        self.assertFalse(os.path.exists(f"{self.ws.books}/Ring, Seth"))
        self.assertEqual(engine.author_merge_plan(self.cfg), [])
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(self.ws.tree(self.ws.books), tree)
        with open(f"{old}/metadata.opf", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

    def test_merge_refuses_a_collision_before_changing_anything(self):
        self.ws.add_library_book("One", "Ring, Seth", 1)
        plan = engine.author_merge_plan(self.cfg)
        write_file(os.path.join(plan[0].new_path, "metadata.opf"), "in the way")
        tree = self.ws.tree(self.ws.books)
        with self.assertRaises(engine.PlanError):
            engine.apply_renames(plan, self.cfg, self.db)
        self.assertEqual(self.ws.tree(self.ws.books), tree)

    def test_merge_keeps_co_authors(self):
        folder = self.ws.add_library_book("Duo", "Ring, Seth", 1)
        with open(f"{folder}/metadata.opf", "w", encoding="utf-8") as f:
            f.write(calibre.write_opf(BookInfo(title="Duo", authors=["Ring, Seth", "Ann Lee"]), 1))
        engine.apply_renames(engine.author_merge_plan(self.cfg), self.cfg, self.db)
        info, _ = read_opf(f"{self.ws.books}/Seth Ring/Duo (1)/metadata.opf")
        self.assertEqual(info.authors, ["Seth Ring", "Ann Lee"])

    def test_edit_renames_and_undo_restores(self):
        old = self.ws.add_library_book("Old Title", "Seth Ring", 9)
        with open(f"{old}/metadata.opf", encoding="utf-8") as f:
            original = f.read()
        book = engine.library_books(self.cfg)[0]
        info = BookInfo(title="New: Title", authors=["Ann Lee"], series="S", series_index=2.0, uuid=book.info.uuid)
        res = engine.edit_book(book, info, self.cfg, self.db)
        self.assertEqual(res.errors, [])
        new = f"{self.ws.books}/Ann Lee/New_ Title (9)"
        self.assertEqual(sorted(os.listdir(new)), ["New_ Title - Ann Lee.epub", "cover.jpg", "metadata.opf"])
        back, book_id = read_opf(f"{new}/metadata.opf")
        self.assertEqual((back.title, back.authors, back.series, book_id), ("New: Title", ["Ann Lee"], "S", 9))
        self.assertFalse(os.path.exists(old))
        engine.undo_operation(res.op_id, self.cfg, self.db)
        with open(f"{old}/metadata.opf", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.assertFalse(os.path.exists(f"{self.ws.books}/Ann Lee"))

    def test_edit_of_a_book_with_no_opf_writes_one_and_undo_removes_it(self):
        folder = f"{self.ws.books}/Seth Ring/Bare (3)"
        make_epub(f"{folder}/Bare - Seth Ring.epub", "Bare")
        book = engine.library_books(self.cfg)[0]
        self.assertFalse(book.has_opf)
        res = engine.edit_book(book, BookInfo(title="Dressed", authors=["Seth Ring"]), self.cfg, self.db)
        new = f"{self.ws.books}/Seth Ring/Dressed (3)"
        self.assertEqual(sorted(os.listdir(new)), ["Dressed - Seth Ring.epub", "metadata.opf"])
        self.assertEqual(read_opf(f"{new}/metadata.opf")[1], 3)
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(os.listdir(folder), ["Bare - Seth Ring.epub"])
        self.assertFalse(os.path.exists(new))

    def test_edit_needs_a_title_and_author(self):
        self.ws.add_library_book("Old", "Seth Ring", 1)
        book = engine.library_books(self.cfg)[0]
        with self.assertRaises(engine.PlanError):
            engine.edit_book(book, BookInfo(title="X", authors=[]), self.cfg, self.db)

    def test_library_listing_ignores_hidden_folders(self):
        self.ws.add_library_book("One", "Seth Ring", 1)
        make_epub(f"{self.ws.books}/.Trash/Gone (2)/g.epub", "Gone")
        self.assertEqual([b.id for b in engine.library_books(self.cfg)], [1])


OPF3 = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Book &amp; Co</dc:title>
    <dc:creator id="a1">Ring, Seth</dc:creator>
    <meta refines="#a1" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#a1" property="file-as">Ring, Seth</meta>
    <dc:creator id="ill">Ann Artist</dc:creator>
    <meta refines="#ill" property="role" scheme="marc:relators">ill</meta>
    <dc:creator id="a2">Second, Person</dc:creator>
    <meta refines="#a2" property="file-as">Second, Person</meta>
  </metadata>
</package>
"""


class TestEpubAuthors(Base):
    def book(self, *authors, **kw) -> str:
        return make_epub(f"{self.ws.root}/b/x.epub", "Title", authors, **kw)

    def entries(self, path):
        with zipfile.ZipFile(path) as z:
            return {i.filename: (i.CRC, i.compress_type) for i in z.infolist() if not i.filename.endswith(".opf")}, z.namelist()

    def test_author_change_keeps_every_other_entry(self):
        path = self.book("Ring, Seth", cover=JPEG)
        before, order = self.entries(path)
        original = epub.write_authors(path, ["Seth Ring"])
        self.assertIn("Ring, Seth", original)
        self.assertEqual(epub.read_authors(path), ["Seth Ring"])
        self.assertEqual(self.entries(path), (before, order))
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)    # mimetype
        self.assertEqual(read_book(path)[0].title, "Title")
        self.assertEqual(os.listdir(os.path.dirname(path)), ["x.epub"])            # no temp file left

    def test_nothing_to_do_leaves_the_file_alone(self):
        path = self.book("Seth Ring")
        stamp = os.stat(path).st_mtime_ns
        self.assertIsNone(epub.write_authors(path, ["Seth Ring"]))
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)

    def test_restore_is_exact(self):
        path = self.book("Ring, Seth")
        original = epub.read_package(path)
        epub.restore_package(path, epub.write_authors(path, ["Seth Ring"]))
        self.assertEqual(epub.read_package(path), original)

    def test_epub3_refinements_and_other_roles(self):
        self.assertEqual(epub.authors_in(OPF3), ["Ring, Seth", "Second, Person"])
        out = epub.set_authors(OPF3, ["Seth Ring", "Person Second"])
        self.assertEqual(epub.authors_in(out), ["Seth Ring", "Person Second"])
        self.assertIn('<meta refines="#a1" property="file-as">Ring, Seth</meta>', out)   # 'Ring, Seth' is the correct sort name
        self.assertIn('<dc:creator id="ill">Ann Artist</dc:creator>', out)
        self.assertIn("<dc:title>Book &amp; Co</dc:title>", out)

    def test_fewer_authors_removes_their_refinements(self):
        out = epub.set_authors(OPF3, ["Seth Ring"])
        self.assertEqual(epub.authors_in(out), ["Seth Ring"])
        self.assertNotIn("#a2", out)
        self.assertIn("Ann Artist", out)
        ET.fromstring(out)

    def test_more_authors_are_added_after_the_last(self):
        opf = '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="d" xmlns:opf="o">\n  <dc:creator opf:role="aut" opf:file-as="Zen and Shin" id="c">Carlo Zen and Shinobu Shinotsuki</dc:creator>\n</metadata></package>'
        out = epub.set_authors(opf, ["Carlo Zen", "Shinobu Shinotsuki"])
        self.assertEqual(epub.authors_in(out), ["Carlo Zen", "Shinobu Shinotsuki"])
        self.assertIn('opf:file-as="Zen, Carlo" id="c"', out)
        self.assertIn('<dc:creator opf:role="aut" opf:file-as="Shinotsuki, Shinobu">Shinobu Shinotsuki</dc:creator>', out)

    def test_names_are_escaped(self):
        path = self.book("Ring, Seth")
        epub.write_authors(path, ["Tom & Jerry <Co>"])
        self.assertEqual(epub.read_authors(path), ["Tom & Jerry <Co>"])

    def test_damaged_book_is_refused_and_left_alone(self):
        path = write_file(f"{self.ws.root}/bad.epub", b"not a zip")
        with self.assertRaises(epub.EpubError):
            epub.write_authors(path, ["Seth Ring"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"not a zip")
        self.assertEqual(os.listdir(self.ws.root).count("bad.epub.kvctl-tmp"), 0)

    def test_failed_verification_keeps_the_original(self):
        path = self.book("Ring, Seth")
        with open(path, "rb") as f:
            data = f.read()
        with mock.patch.object(epub.os, "replace", side_effect=OSError(28, "No space left", path)):
            with self.assertRaises(epub.EpubError):
                epub.write_authors(path, ["Seth Ring"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["x.epub"])

    def test_import_writes_authors_and_undo_restores_them(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Book", ["Ring, Seth"])
        res = self.apply(self.plan(self.folder_source("B")))
        self.assertEqual(res.errors, [])
        self.assertEqual(epub.read_authors(f"{self.ws.books}/Seth Ring/Book (1)/Book - Seth Ring.epub"), ["Seth Ring"])
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_authors(f"{self.ws.inbox}/B/a.epub"), ["Ring, Seth"])

    def test_unreadable_epub_still_imports_and_says_so(self):
        write_file(f"{self.ws.inbox}/B/Title - Seth Ring.epub", b"not a zip")
        res = self.apply(self.plan(self.folder_source("B")))
        self.assertEqual(res.imported, 1)
        self.assertIn("not changed", res.errors[0])

    def test_edit_writes_authors_only_when_they_change(self):
        folder = self.ws.add_library_book("Old", "Seth Ring", 1)
        epub_path = f"{folder}/Old - Seth Ring.epub"
        stamp = os.stat(epub_path).st_mtime_ns
        book = engine.library_books(self.cfg)[0]
        engine.edit_book(book, BookInfo(title="New", authors=["Seth Ring"]), self.cfg, self.db)
        moved = f"{self.ws.books}/Seth Ring/New (1)/New - Seth Ring.epub"
        self.assertEqual(os.stat(moved).st_mtime_ns, stamp)
        book = engine.library_books(self.cfg)[0]
        res = engine.edit_book(book, BookInfo(title="New", authors=["Ann Lee"]), self.cfg, self.db)
        self.assertEqual(epub.read_authors(f"{self.ws.books}/Ann Lee/New (1)/New - Ann Lee.epub"), ["Ann Lee"])
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_authors(moved), ["Seth Ring"])

    def test_merge_makes_one_person_read_the_same_everywhere(self):
        # A: reversed folder and EPUB. B: right folder, reversed EPUB. C: already right.
        a = self.ws.add_library_book("Alpha", "Ring, Seth", 1)
        b = self.ws.add_library_book("Beta", "Seth Ring", 2)
        self.ws.add_library_book("Gamma", "Seth Ring", 3)
        epub.write_authors(f"{b}/Beta - Seth Ring.epub", ["Ring, Seth"])
        self.assertEqual(epub.read_authors(f"{a}/Alpha - Ring, Seth.epub"), ["Ring, Seth"])
        plan = engine.author_merge_plan(self.cfg)
        self.assertEqual(sorted(r.book.id for r in plan), [1, 2])
        res = engine.apply_renames(plan, self.cfg, self.db)
        self.assertEqual(res.errors, [])
        for book in engine.library_books(self.cfg):
            self.assertEqual(epub.read_authors(os.path.join(book.path, book.files[0])), ["Seth Ring"])
        self.assertEqual(engine.author_merge_plan(self.cfg), [])
        tree_after = self.ws.tree(self.ws.books)
        self.assertIn("Seth Ring/Alpha (1)/Alpha - Seth Ring.epub", tree_after)
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_authors(f"{a}/Alpha - Ring, Seth.epub"), ["Ring, Seth"])
        self.assertEqual(epub.read_authors(f"{b}/Beta - Seth Ring.epub"), ["Ring, Seth"])

    def test_merge_splits_a_joint_field_when_it_is_not_the_usual_form(self):
        tanya = self.ws.add_library_book("Tanya One", "Carlo Zen", 1)
        self.ws.add_library_book("Tanya Two", "Shinobu Shinotsuki", 2)
        overlord = self.ws.add_library_book("Overlord", "Kugane Maruyama", 3)
        epub.write_authors(f"{tanya}/Tanya One - Carlo Zen.epub", ["Carlo Zen and Shinobu Shinotsuki"])
        epub.write_authors(f"{overlord}/Overlord - Kugane Maruyama.epub", ["Kugane Maruyama & so-bin"])
        plan = engine.author_merge_plan(self.cfg)
        self.assertEqual([r.book.id for r in plan], [1])
        self.assertEqual(plan[0].epub_authors, {"Tanya One - Carlo Zen.epub": ["Carlo Zen", "Shinobu Shinotsuki"]})
        self.assertEqual(plan[0].new_path, tanya)      # nothing moves
        res = engine.apply_renames(plan, self.cfg, self.db)
        self.assertEqual(res.errors, [])
        self.assertEqual(epub.read_authors(f"{tanya}/Tanya One - Carlo Zen.epub"), ["Carlo Zen", "Shinobu Shinotsuki"])
        self.assertEqual(epub.read_authors(f"{overlord}/Overlord - Kugane Maruyama.epub"), ["Kugane Maruyama & so-bin"])
        self.assertEqual(engine.author_merge_plan(self.cfg), [])

    def test_merge_reports_a_book_it_cannot_rewrite_and_carries_on(self):
        self.ws.add_library_book("Alpha", "Ring, Seth", 1)
        self.ws.add_library_book("Beta", "Arina, Bertt", 2)
        plan = engine.author_merge_plan(self.cfg)
        real, calls = epub.write_metadata, []

        def first_fails(path, *args):
            calls.append(path)
            if len(calls) == 1:
                raise epub.EpubError("disk trouble")
            return real(path, *args)
        with mock.patch.object(epub, "write_metadata", first_fails):
            res = engine.apply_renames(plan, self.cfg, self.db)
        self.assertEqual(res.imported, 2)
        self.assertEqual(len(res.errors), 1)
        self.assertIn("disk trouble", res.errors[0])


class TestLookup(unittest.TestCase):
    def lookup(self, results, error=None):
        lk = Lookup(None)

        class Fake:
            def search(self, title, author=""):
                if error:
                    raise error
                return results
        lk.openlibrary = Fake()
        return lk

    def test_variants(self):
        self.assertEqual(title_variants("Book: The Subtitle (Series 2)"),
                         ["Book: The Subtitle (Series 2)", "Book", "Book: The Subtitle"])

    def test_clear_match_is_accepted(self):
        lk = self.lookup([BookMatch("Open Library", "Neuromancer", ["William Gibson"], 1984), BookMatch("Open Library", "Burning Chrome", ["William Gibson"])])
        best = lk.best("Neuromancer", ["Gibson, William"])
        self.assertEqual(best.title, "Neuromancer")

    def test_weak_match_is_rejected(self):
        lk = self.lookup([BookMatch("Open Library", "Savage Gardens", ["Someone Else"])])
        self.assertIsNone(lk.best("Savage Webs", ["Seth Ring"]))

    def test_network_problem_is_reported_not_raised(self):
        from kutil.net import NetError
        lk = self.lookup([], NetError("openlibrary.org: down"))
        self.assertEqual(lk.search("Book", ["Author"]), [])
        self.assertEqual(lk.errors, ["openlibrary.org: down"])


if __name__ == "__main__":
    unittest.main()
