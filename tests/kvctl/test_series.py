"""Keeping series and joint author names together, inside the EPUB as Kavita reads them."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

from kvctl import calibre, cli, config, engine, epub, series
from kvctl.calibre import BookInfo, read_opf
from kvctl.series import Item, assign, guess
from tests.kvctl.helpers import Workspace, make_epub
from tests.kvctl.test_engine import Base

EPUB3 = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>T</dc:title>
    <meta name="cover" content="img"/>
    <dc:creator id="a">Ann Lee</dc:creator>
    <meta property="belongs-to-collection" id="c1">Old Saga</meta>
    <meta refines="#c1" property="collection-type">series</meta>
    <meta refines="#c1" property="group-position">3</meta>
</metadata></package>"""


class TestGuess(Base):
    def test_titles_from_a_real_library(self):
        cases = {
            "Savage Webs: A LitRPG Cultivation Series (Apocalypse Cultivation Book 2)": ("Apocalypse Cultivation", 2.0),
            "The Saga of Tanya the Evil, Vol. 1: Deus lo Vult": ("The Saga of Tanya the Evil", 1.0),
            "That Time I Got Reincarnated as a Slime, Vol. 2 [light novel]": ("That Time I Got Reincarnated as a Slime", 2.0),
            "Combat Healer 2: Holstaur Essence Archer": ("Combat Healer", 2.0),
            "Blue Core: Book One": ("Blue Core", 1.0),
            "Ordinal Trail: Otherworld Adventures book 01": ("Ordinal Trail: Otherworld Adventures", 1.0),
            "Jake's Magical Market 3: Home Sweet Home": ("Jake's Magical Market", 3.0),
            "Germination: Battle Mage Farmer": None,
            "Horus Rising": None,
            "Meditations": None,
        }
        for title, expected in cases.items():
            self.assertEqual(guess(title), expected, title)


class TestAssign(Base):
    def item(self, key, title, author="ann", series_name="", index=None, date=""):
        return Item(key, author, title, series_name, index, date)

    def test_a_lone_guess_counts_for_nothing(self):
        self.assertEqual(assign([self.item(1, "Catch 22: A Novel"), self.item(2, "Other Book")]), {})

    def test_same_series_needs_the_same_author(self):
        self.assertEqual(assign([self.item(1, "Saga, Vol. 1", "ann"), self.item(2, "Saga, Vol. 2", "bob")]), {})

    def test_joins_an_existing_series_and_keeps_its_name(self):
        found = assign([self.item(1, "Reincarnated Stuff", series_name="Reincarnated as a Slime", index=3.0),
                        self.item(2, "That Time I Got Reincarnated as a Slime, Vol. 1 [light novel]")])
        self.assertEqual((found[2].series, found[2].index, found[2].number_guessed), ("Reincarnated as a Slime", 1.0, False))
        self.assertNotIn(1, found)

    def test_a_book_in_a_series_is_never_changed(self):
        found = assign([self.item(1, "Saga, Vol. 9", series_name="Saga", index=2.0), self.item(2, "Saga, Vol. 3")])
        self.assertNotIn(1, found)
        self.assertEqual(found[2].index, 3.0)

    def test_unnumbered_books_take_the_free_places(self):
        found = assign([self.item(1, "Market 3: Home", date="2024-04-18"),
                        self.item(2, "Market: A Trek", date="2024-01-04"),
                        self.item(3, "Market", date="2021-10-05")])
        self.assertEqual({k: (a.index, a.number_guessed) for k, a in found.items()},
                         {1: (3.0, False), 2: (2.0, True), 3: (1.0, True)})

    def test_subtitle_names_the_series(self):
        found = assign([self.item(1, "Domestication (Battle Mage Farmer Book 1)"), self.item(2, "Germination: Battle Mage Farmer")])
        self.assertEqual((found[2].series, found[2].index, found[2].number_guessed), ("Battle Mage Farmer", 2.0, True))

    def test_a_series_with_no_number_gets_one(self):
        found = assign([self.item(1, "A", series_name="Saga", index=1.0), self.item(2, "B", series_name="Saga")])
        self.assertEqual(found[2].index, 2.0)

    def test_short_names_do_not_match_inside_others(self):
        found = assign([self.item(1, "The Fall", series_name="Fall", index=1.0), self.item(2, "Rise and Fall, Vol. 2")])
        self.assertNotIn(2, found)


class TestSeriesInsideTheBook(Base):
    def book(self, **kw) -> str:
        return make_epub(f"{self.ws.root}/b/x.epub", "Title", ["Ann Lee"], **kw)

    def test_adds_what_kavita_needs(self):
        path = self.book()
        self.assertEqual(epub.read_series(path), ("", None))
        before = {i.filename: i.CRC for i in zipfile.ZipFile(path).infolist() if not i.filename.endswith(".opf")}
        original = epub.write_metadata(path, series="Saga & Co", index=2.5)
        self.assertEqual(epub.read_series(path), ("Saga & Co", 2.5))
        self.assertEqual({i.filename: i.CRC for i in zipfile.ZipFile(path).infolist() if not i.filename.endswith(".opf")}, before)
        self.assertEqual(epub.read_authors(path), ["Ann Lee"])
        epub.restore_package(path, original)
        self.assertEqual(epub.read_series(path), ("", None))

    def test_no_number_means_one(self):
        path = self.book()
        epub.write_metadata(path, series="Saga")
        self.assertEqual(epub.read_series(path), ("Saga", 1.0))

    def test_replaces_calibre_entries(self):
        path = self.book(series="Old", index=1)
        epub.write_metadata(path, series="New", index=4.0)
        self.assertEqual(epub.read_series(path), ("New", 4.0))
        self.assertEqual(epub.read_package(path).count("calibre:series\""), 1)

    def test_no_change_leaves_the_file_alone(self):
        path = self.book(series="Saga", index=2)
        stamp = os.stat(path).st_mtime_ns
        self.assertIsNone(epub.write_metadata(path, series="Saga", index=2.0))
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)

    def test_removes_a_series(self):
        path = self.book(series="Old", index=1)
        epub.write_metadata(path, series="")
        self.assertEqual(epub.read_series(path), ("", None))
        self.assertNotIn("calibre:series", epub.read_package(path))

    def test_epub3_collection(self):
        self.assertEqual(epub.series_in(EPUB3), ("Old Saga", 3.0))
        out = epub.set_series(EPUB3, "New Saga", 5.0)
        self.assertEqual(epub.series_in(out), ("New Saga", 5.0))
        self.assertIn(">New Saga</meta>", out)
        self.assertIn('property="group-position">5</meta>', out)
        ET.fromstring(out)
        empty = epub.set_series(EPUB3, "", None)
        self.assertEqual(epub.series_in(empty), ("", None))
        self.assertNotIn("#c1", empty)
        self.assertIn('name="cover"', empty)

    def test_another_collection_is_left_alone(self):
        opf = EPUB3.replace('<dc:title>T</dc:title>', '<dc:title>T</dc:title>\n<meta property="belongs-to-collection" id="box">Box Set</meta>')
        out = epub.set_series(opf, "New Saga", 5.0)
        self.assertEqual(epub.series_in(out), ("New Saga", 5.0))
        self.assertIn(">Box Set</meta>", out)

    def test_self_closed_meta_does_not_hide_the_next_one(self):
        opf = """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:creator id="a">Ann Lee</dc:creator>
          <dc:creator id="ill">Art Ist</dc:creator>
          <meta name="cover" content="img"/>
          <meta refines="#ill" property="role">ill</meta>
          <meta name="tag" content="a/b"/>
          <meta refines="#a" property="file-as">Lee, Ann</meta>
        </metadata></package>"""
        self.assertEqual(epub.authors_in(opf), ["Ann Lee"])
        out = epub.set_authors(opf, ["Ann B. Lee"])
        self.assertIn('property="file-as">Lee, Ann B.</meta>', out)
        self.assertIn(">Art Ist</dc:creator>", out)


class TestLibrarySeries(Base):
    def add(self, title, author, book_id, series_name="", index=None):
        folder = self.ws.add_library_book(title, author, book_id)
        path = os.path.join(folder, calibre.file_stem(title, author) + ".epub")
        if series_name:
            epub.write_metadata(path, series=series_name, index=index)
            with open(f"{folder}/metadata.opf", "w", encoding="utf-8") as f:
                f.write(calibre.write_opf(BookInfo(title=title, authors=[author], series=series_name, series_index=index), book_id))
        return folder, path

    def test_plan_apply_undo(self):
        _, p1 = self.add("Saga, Vol. 1", "Ann Lee", 1)
        f2, p2 = self.add("Saga, Vol. 2", "Ann Lee", 2)
        _, solo = self.add("Solo, Vol. 7", "Ann Lee", 3)
        with open(f"{f2}/metadata.opf", encoding="utf-8") as f:
            opf_before = f.read()
        plan = engine.series_fix_plan(self.cfg)
        self.assertEqual([(f.book.id, f.series, f.index) for f in plan], [(1, "Saga", 1.0), (2, "Saga", 2.0)])
        res = engine.apply_series(plan, self.cfg, self.db)
        self.assertEqual((res.imported, res.errors), (2, []))
        self.assertEqual(epub.read_series(p2), ("Saga", 2.0))
        info, book_id = read_opf(f"{f2}/metadata.opf")
        self.assertEqual((info.series, info.series_index, info.title, book_id), ("Saga", 2.0, "Saga, Vol. 2", 2))
        self.assertEqual(epub.read_series(solo), ("", None))
        self.assertEqual(engine.series_fix_plan(self.cfg), [])
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_series(p1), ("", None))
        with open(f"{f2}/metadata.opf", encoding="utf-8") as f:
            self.assertEqual(f.read(), opf_before)

    def test_other_formats_are_not_touched(self):
        folder = f"{self.ws.books}/Ann Lee/Saga, Vol. 1 (1)"
        os.makedirs(folder)
        with open(f"{folder}/Saga, Vol. 1 - Ann Lee.mobi", "wb") as f:
            f.write(b"mobi")
        self.add("Saga, Vol. 2", "Ann Lee", 2)
        self.assertEqual([f.book.id for f in engine.series_fix_plan(self.cfg)], [2])

    def test_edit_writes_series_inside_and_undo_restores(self):
        folder, path = self.add("Book", "Ann Lee", 1)
        book = engine.library_books(self.cfg)[0]
        res = engine.edit_book(book, BookInfo(title="Book", authors=["Ann Lee"], series="Saga", series_index=2.0), self.cfg, self.db)
        self.assertEqual(res.errors, [])
        self.assertEqual(epub.read_series(path), ("Saga", 2.0))
        engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_series(path), ("", None))
        book = engine.library_books(self.cfg)[0]
        engine.edit_book(book, BookInfo(title="Book", authors=["Ann Lee"], series="Saga", series_index=2.0), self.cfg, self.db)
        book = engine.library_books(self.cfg)[0]
        engine.edit_book(book, BookInfo(title="Book", authors=["Ann Lee"]), self.cfg, self.db)
        self.assertEqual(epub.read_series(path), ("", None))

    def test_import_joins_an_existing_series(self):
        self.add("First", "Kaizer Wolf", 1, "Combat Healer", 1.0)
        make_epub(f"{self.ws.inbox}/B/x.epub", "Combat Healer 2: Archer", ["Wolf, Kaizer"])
        plan = self.plan(self.folder_source("B"))
        book = plan.books[0]
        self.assertEqual((book.info.series, book.info.series_index), ("Combat Healer", 2.0))
        self.assertIn("taken from the title", book.warning)
        self.assertEqual(self.apply(plan).errors, [])
        path = f"{self.ws.books}/Kaizer Wolf/Combat Healer 2_ Archer (2)/Combat Healer 2_ Archer - Kaizer Wolf.epub"
        self.assertEqual(epub.read_series(path), ("Combat Healer", 2.0))

    def test_changing_the_guess_in_review_clears_its_warning(self):
        self.add("First", "Ann Lee", 1, "Saga", 1.0)
        make_epub(f"{self.ws.inbox}/B/x.epub", "Saga 2: Next", ["Ann Lee"])
        plan = self.plan(self.folder_source("B"))
        plan.books[0].info.series = "Better Name"
        engine.check_plan(plan, self.cfg, self.db)
        self.assertNotIn("taken from the title", plan.books[0].warning)

    def test_import_of_two_volumes_together_groups_them(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Saga, Vol. 1", ["Ann Lee"])
        make_epub(f"{self.ws.inbox}/B/b.epub", "Saga, Vol. 2", ["Ann Lee"])
        plan = self.plan(self.folder_source("B"))
        self.assertEqual(sorted((b.info.series, b.info.series_index) for b in plan.books), [("Saga", 1.0), ("Saga", 2.0)])

    def test_import_of_a_stray_numbered_title_stays_out_of_any_series(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Catch 22: A Novel", ["Joe Heller"])
        plan = self.plan(self.folder_source("B"))
        self.assertEqual(plan.books[0].info.series, "")
        self.assertNotIn("taken from the title", plan.books[0].warning)

    def test_import_keeps_a_series_the_book_already_has(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Whatever", ["Ann Lee"], series="Saga", index=4)
        self.apply(self.plan(self.folder_source("B")))
        self.assertEqual(epub.read_series(f"{self.ws.books}/Ann Lee/Whatever (1)/Whatever - Ann Lee.epub"), ("Saga", 4.0))


class TestImportBringsItsPartners(Base):
    def import_book(self, title, author="Bruce Sentar", tweak=None):
        make_epub(f"{self.ws.inbox}/{title}/a.epub", title, [author])
        plan = self.plan(self.folder_source(title))
        if tweak:
            tweak(plan)
            engine.check_plan(plan, self.cfg, self.db)
        return plan, self.apply(plan)

    def path(self, title, book_id, author="Bruce Sentar"):
        return f"{self.ws.books}/{author}/{title} ({book_id})/{title} - {author}.epub"

    def test_later_volume_first_then_earlier_one_ends_up_together(self):
        self.import_book("Dungeon Diving 102")
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("", None))    # alone, nothing to group with
        plan, res = self.import_book("Dungeon Diving 101")
        self.assertEqual(res.errors, [])
        self.assertEqual([(f.book.id, f.series, f.index) for f in plan.series_partners], [(1, "Dungeon Diving", 102.0)])
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 101", 2)), ("Dungeon Diving", 101.0))
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("Dungeon Diving", 102.0))
        info, _ = read_opf(f"{self.ws.books}/Bruce Sentar/Dungeon Diving 102 (1)/metadata.opf")
        self.assertEqual((info.series, info.series_index, info.title), ("Dungeon Diving", 102.0, "Dungeon Diving 102"))
        self.assertEqual(engine.series_fix_plan(self.cfg), [])

    def test_undoing_the_import_takes_the_partner_back_out(self):
        self.import_book("Dungeon Diving 102")
        first_opf = f"{self.ws.books}/Bruce Sentar/Dungeon Diving 102 (1)/metadata.opf"
        with open(first_opf, encoding="utf-8") as f:
            before = f.read()
        _, res = self.import_book("Dungeon Diving 101")
        self.assertEqual(engine.undo_operation(res.op_id, self.cfg, self.db)[1], [])
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("", None))
        with open(first_opf, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(os.path.exists(f"{self.ws.inbox}/Dungeon Diving 101/a.epub"))

    def test_a_skipped_book_brings_nobody(self):
        self.import_book("Dungeon Diving 102")
        make_epub(f"{self.ws.inbox}/P/a.epub", "Dungeon Diving 101", ["Bruce Sentar"])
        make_epub(f"{self.ws.inbox}/P/b.epub", "Unrelated", ["Bruce Sentar"])
        plan = self.plan(self.folder_source("P"))
        self.assertEqual(len(plan.series_partners), 1)
        next(b for b in plan.books if b.info.title == "Dungeon Diving 101").skip = True
        engine.check_plan(plan, self.cfg, self.db)
        self.assertEqual(plan.series_partners, [])
        self.apply(plan)
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("", None))

    def test_clearing_the_series_in_review_brings_nobody(self):
        self.import_book("Dungeon Diving 102")

        def clear(plan):
            plan.books[0].info.series, plan.books[0].info.series_index = "", None
        plan, _ = self.import_book("Dungeon Diving 101", tweak=clear)
        self.assertEqual(plan.series_partners, [])
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("", None))

    def test_a_new_book_with_its_own_series_brings_the_others(self):
        self.import_book("Dungeon Diving 102")
        make_epub(f"{self.ws.inbox}/N/a.epub", "Dungeon Diving 103", ["Bruce Sentar"], series="Dungeon Diving", index=103)
        plan = self.plan(self.folder_source("N"))
        self.assertEqual([f.book.id for f in plan.series_partners], [1])
        self.apply(plan)
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("Dungeon Diving", 102.0))

    def test_other_ungrouped_books_are_left_alone(self):
        other = self.ws.add_library_book("Saga, Vol. 1", "Ann Lee", 50)
        self.ws.add_library_book("Saga, Vol. 2", "Ann Lee", 51)
        same_author = self.ws.add_library_book("Other Tale, Vol. 1", "Bruce Sentar", 52)
        self.ws.add_library_book("Other Tale, Vol. 2", "Bruce Sentar", 53)
        self.ws.add_library_book("Dungeon Diving 102", "Bruce Sentar", 54)
        plan, res = self.import_book("Dungeon Diving 101")
        self.assertEqual([f.book.id for f in plan.series_partners], [54])
        self.assertEqual(res.errors, [])
        self.assertEqual(epub.read_series(f"{other}/Saga, Vol. 1 - Ann Lee.epub"), ("", None))
        self.assertEqual(epub.read_series(f"{same_author}/Other Tale, Vol. 1 - Bruce Sentar.epub"), ("", None))

    def test_a_different_author_is_not_a_partner(self):
        self.ws.add_library_book("Dungeon Diving 102", "Someone Else", 50)
        plan = self.plan(self.source_for("Dungeon Diving 101"))
        self.assertEqual(plan.series_partners, [])
        self.assertEqual(plan.books[0].info.series, "")

    def source_for(self, title):
        make_epub(f"{self.ws.inbox}/{title}/a.epub", title, ["Bruce Sentar"])
        return self.folder_source(title)

    def test_a_partner_that_cannot_be_rewritten_is_reported_and_the_import_stands(self):
        self.import_book("Dungeon Diving 102")
        make_epub(f"{self.ws.inbox}/N/a.epub", "Dungeon Diving 101", ["Bruce Sentar"])
        plan = self.plan(self.folder_source("N"))
        real = epub.write_metadata

        def refuse_partner(path, *args):
            if "102" in path:
                raise epub.EpubError("disk trouble")
            return real(path, *args)
        with mock.patch.object(epub, "write_metadata", refuse_partner):
            res = self.apply(plan)
        self.assertEqual(res.imported, 1)
        self.assertIn("disk trouble", res.errors[0])
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 101", 2)), ("Dungeon Diving", 101.0))

    def test_plan_command_names_the_partner(self):
        self.import_book("Dungeon Diving 102")
        make_epub(f"{self.ws.inbox}/N/a.epub", "Dungeon Diving 101", ["Bruce Sentar"])
        out = TestCommand.run_cli(self, "--offline", "plan", "N")[1]
        self.assertIn("Also adds Dungeon Diving 102 to Dungeon Diving, as #102", out)
        self.assertEqual(epub.read_series(self.path("Dungeon Diving 102", 1)), ("", None))


class TestUndoOrder(Base):
    def test_undoing_a_merge_cannot_wipe_a_later_series_change(self):
        folder = self.ws.add_library_book("Saga, Vol. 1", "Ring, Seth", 1)
        self.ws.add_library_book("Saga, Vol. 2", "Ring, Seth", 2)
        merge = engine.apply_renames(engine.author_merge_plan(self.cfg), self.cfg, self.db)
        grouped = engine.apply_series(engine.series_fix_plan(self.cfg), self.cfg, self.db)
        path = f"{self.ws.books}/Seth Ring/Saga, Vol. 1 (1)/Saga, Vol. 1 - Seth Ring.epub"
        self.assertEqual(epub.read_series(path), ("Saga", 1.0))
        tree = self.ws.tree(self.ws.books)
        with self.assertRaises(engine.PlanError) as caught:
            engine.undo_operation(merge.op_id, self.cfg, self.db)
        self.assertIn(f"operation {grouped.op_id} changed the same books", str(caught.exception))
        self.assertEqual(self.ws.tree(self.ws.books), tree)
        self.assertEqual(epub.read_series(path), ("Saga", 1.0))
        engine.undo_operation(grouped.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_series(path), ("", None))
        engine.undo_operation(merge.op_id, self.cfg, self.db)
        self.assertEqual(epub.read_authors(f"{folder}/Saga, Vol. 1 - Ring, Seth.epub"), ["Ring, Seth"])

    def test_a_later_operation_on_other_books_does_not_block_undo(self):
        self.ws.add_library_book("One", "Ring, Seth", 1)
        merge = engine.apply_renames(engine.author_merge_plan(self.cfg), self.cfg, self.db)
        self.ws.add_library_book("Saga, Vol. 1", "Ann Lee", 2)
        self.ws.add_library_book("Saga, Vol. 2", "Ann Lee", 3)
        engine.apply_series(engine.series_fix_plan(self.cfg), self.cfg, self.db)
        self.assertEqual(engine.undo_operation(merge.op_id, self.cfg, self.db)[1], [])

    def test_an_undone_later_operation_no_longer_blocks(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Book", ["Ann Lee"])
        imported = self.apply(self.plan(self.folder_source("B")))
        book = engine.library_books(self.cfg)[0]
        edit = engine.edit_book(book, BookInfo(title="Book", authors=["Ann Lee"], series="S", series_index=1.0), self.cfg, self.db)
        with self.assertRaises(engine.PlanError):
            engine.undo_operation(imported.op_id, self.cfg, self.db)
        engine.undo_operation(edit.op_id, self.cfg, self.db)
        self.assertEqual(engine.undo_operation(imported.op_id, self.cfg, self.db)[1], [])


class TestJointNamesStayTogether(Base):
    def overlord(self, book_id, inner=("Kugane Maruyama & so-bin",)):
        title = f"Overlord, Vol. {book_id}"
        folder = self.ws.add_library_book(title, "Kugane Maruyama", book_id)
        path = f"{folder}/{calibre.file_stem(title, 'Kugane Maruyama')}.epub"
        epub.write_authors(path, list(inner))
        with open(f"{folder}/metadata.opf", "w", encoding="utf-8") as f:
            f.write(calibre.write_opf(BookInfo(title=title, authors=["Kugane Maruyama", "so-bin"]), book_id))
        return path

    def test_merge_leaves_the_joint_name_alone(self):
        self.overlord(1)
        self.overlord(2)
        self.assertEqual(engine.author_merge_plan(self.cfg), [])

    def test_a_later_book_naming_one_of_them_alone_does_not_split_the_rest(self):
        self.overlord(1)
        self.overlord(2)
        self.ws.add_library_book("Other Work", "Kugane Maruyama", 3)
        self.assertEqual(engine.author_merge_plan(self.cfg), [])

    def test_import_keeps_a_new_volume_in_the_same_form(self):
        self.overlord(1)
        make_epub(f"{self.ws.inbox}/B/a.epub", "Overlord, Vol. 17", ["Kugane Maruyama & so-bin"])
        plan = self.plan(self.folder_source("B"))
        self.assertEqual(plan.books[0].info.authors, ["Kugane Maruyama", "so-bin"])     # folders still use the first name
        self.assertEqual(self.apply(plan).errors, [])
        path = f"{self.ws.books}/Kugane Maruyama/Overlord, Vol. 17 (2)/Overlord, Vol. 17 - Kugane Maruyama.epub"
        self.assertEqual(epub.read_authors(path), ["Kugane Maruyama & so-bin"])

    def test_import_joins_the_same_people_listed_separately(self):
        self.overlord(1)
        make_epub(f"{self.ws.inbox}/B/a.epub", "Overlord, Vol. 18", ["Kugane Maruyama", "so-bin"])
        self.apply(self.plan(self.folder_source("B")))
        path = f"{self.ws.books}/Kugane Maruyama/Overlord, Vol. 18 (2)/Overlord, Vol. 18 - Kugane Maruyama.epub"
        self.assertEqual(epub.read_authors(path), ["Kugane Maruyama & so-bin"])

    def test_a_joint_name_new_to_the_library_is_split(self):
        make_epub(f"{self.ws.inbox}/B/a.epub", "Duo", ["Ann Lee & Bob Ray"])
        self.apply(self.plan(self.folder_source("B")))
        self.assertEqual(epub.read_authors(f"{self.ws.books}/Ann Lee/Duo (1)/Duo - Ann Lee.epub"), ["Ann Lee", "Bob Ray"])

    def test_authors_changed_in_review_are_written_as_chosen(self):
        self.overlord(1)
        make_epub(f"{self.ws.inbox}/B/a.epub", "Overlord, Vol. 19", ["Kugane Maruyama & so-bin"])
        plan = self.plan(self.folder_source("B"))
        plan.books[0].info.authors = ["Someone Else"]
        self.apply(plan)
        self.assertEqual(epub.read_authors(f"{self.ws.books}/Someone Else/Overlord, Vol. 19 (2)/Overlord, Vol. 19 - Someone Else.epub"), ["Someone Else"])

    def test_series_changes_leave_authors_alone(self):
        path = self.overlord(1)
        self.overlord(2)
        res = engine.apply_series(engine.series_fix_plan(self.cfg), self.cfg, self.db)
        self.assertEqual(res.errors, [])
        self.assertEqual(epub.read_authors(path), ["Kugane Maruyama & so-bin"])
        self.assertEqual(epub.read_series(path), ("Overlord", 1.0))


class TestCommand(Base):
    def run_cli(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"KVCTL_CONFIG": f"{self.ws.root}/c.toml", "TRCTL_CONFIG": "/nonexistent",
                                          "TRCTL_URL": "http://127.0.0.1:9/transmission/rpc"}):
            config.save(self.cfg)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--color", "never", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_shows_then_applies(self):
        folder = self.ws.add_library_book("Market", "Ann Lee", 1)
        self.ws.add_library_book("Market 2: More", "Ann Lee", 2)
        code, out, _ = self.run_cli("fix-series")
        self.assertEqual(code, 0)
        self.assertIn("Market", out)
        self.assertIn("(number guessed)", out)
        self.assertIn("Nothing changed", out)
        self.assertEqual(epub.read_series(f"{folder}/Market - Ann Lee.epub"), ("", None))
        code, out, _ = self.run_cli("fix-series", "--apply", "-y")
        self.assertEqual(code, 0, out)
        self.assertIn("Grouped 2 book(s)", out)
        self.assertEqual(epub.read_series(f"{folder}/Market - Ann Lee.epub"), ("Market", 1.0))
        self.assertIn("No books need grouping", self.run_cli("fix-series")[1])
