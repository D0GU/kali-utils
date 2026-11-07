"""plexsort tests. Everything runs in temp folders against trctl's fake Transmission daemon;
no network access and no real torrents are touched."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # kali-utils root
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "trctl")]  # trctl's fake daemon

from fake_daemon import FakeDaemon, make_torrent  # noqa: E402
from trctl.rpc import Client  # noqa: E402

from plexsort import engine, naming  # noqa: E402
from plexsort.db import DB  # noqa: E402
from plexsort.parse import Plan, build_items, clean_title  # noqa: E402
from plexsort.providers import AnimeMap, Episode, Http, Match, apply_match, query_variants, score_match  # noqa: E402

PACK = "(CBB) Space Drill Saga - The Complete Series (BD 1080p)(Dual-Audio)(HEVC-x265-10bit)"
PACK_FILES = [
    "01. Series/01 - Bust Through.mkv", "01. Series/02 - Pilot That Thing.mkv", "01. Series/03 - Two Faces.mkv",
    "01. Series/03 - Two Faces.en.srt",
    "02. Movies/(CBB) Space Drill Saga The Movie - 01 (Gurren-hen).mkv",
    "03. Extra/01. NCOP/NCOP 01.mkv", "03. Extra/04. OVA/06 - Hot Tub.mkv",
    "Torrent Description (READ Me).txt", "sample.mkv",
]


def make_tree(base: str, name: str, files: list[str]) -> str:
    root = os.path.join(base, name)
    for rel in files:
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(rel.encode() * 10)
    return root


class TestParse(unittest.TestCase):
    def test_a_year_does_not_cut_an_extra_or_episode_name(self):
        extras = ["Extras/[Judas] Overlord 2 - At Anime Expo 2018 A Message From Minoru Ashina.mkv",
                  "Extras/[Judas] Overlord 2 - At Anime Expo 2018 Interview With Kugane Maruyama & Naoyuki Itou.mkv"]
        items = build_items("Overlord", [(f, 10 ** 8) for f in extras])[0]
        self.assertEqual(sorted(i.extra_name for i in items),
                         ["Overlord 2 - At Anime Expo 2018 A Message From Minoru Ashina",
                          "Overlord 2 - At Anime Expo 2018 Interview With Kugane Maruyama & Naoyuki Itou"])
        self.assertEqual({i.kind for i in items}, {"extra"})
        ep = build_items("Show", [("Show.S01E02.The.Summer.of.1969.Part.Two.1080p.mkv", 2_000_000_000)])[0][0]
        self.assertEqual(ep.ep_title, "The Summer of 1969 Part Two")

    def test_release_junk_still_goes_from_those_names(self):
        ep = build_items("Show", [("Show.S01E02.Pilot.1080p.WEB-DL.mkv", 2_000_000_000)])[0][0]
        self.assertEqual(ep.ep_title, "Pilot")
        tagged = build_items("Show", [("Extras/Making Of (2019) [BD 1080p].mkv", 10 ** 8)])[0][0]
        self.assertEqual(tagged.extra_name, "Making Of")
        self.assertEqual(clean_title("Some.Movie.2019.1080p")[1], 2019)     # movies still end at the year

    def items(self, root, files):
        items, leftovers, incomplete = build_items(root, [(f, 2_000_000_000) for f in files])
        return {i.src: i for i in items}, leftovers, incomplete

    def test_clean_title(self):
        self.assertEqual(clean_title(PACK), ("Space Drill Saga", None))
        self.assertEqual(clean_title("NINJA SCROLL (1993-2003) - Complete Movie and Anime TV Series - 1080p"),
                         ("Ninja Scroll", 1993))
        self.assertEqual(clean_title("Inception.2010.1080p.BluRay.x264-GRP"), ("Inception", 2010))
        self.assertEqual(clean_title("Ninja Scroll, with Commentary (1993 BluRay - 1080p TRI Audio)"),
                         ("Ninja Scroll, with Commentary", 1993))

    def test_mixed_anime_pack(self):
        items, leftovers, _ = self.items(PACK, PACK_FILES)
        ep = items["01. Series/02 - Pilot That Thing.mkv"]
        self.assertEqual((ep.kind, ep.category, ep.title, ep.season, ep.episode, ep.ep_title),
                         ("episode", "show", "Space Drill Saga", 1, 2, "Pilot That Thing"))
        self.assertTrue(ep.absolute and ep.anime)
        movie = items["02. Movies/(CBB) Space Drill Saga The Movie - 01 (Gurren-hen).mkv"]
        self.assertEqual((movie.kind, movie.category), ("movie", "movie"))
        ncop = items["03. Extra/01. NCOP/NCOP 01.mkv"]
        self.assertEqual((ncop.kind, ncop.category, ncop.title, ncop.extra_name), ("extra", "show", "Space Drill Saga", "NCOP 01"))
        ova = items["03. Extra/04. OVA/06 - Hot Tub.mkv"]
        self.assertEqual((ova.kind, ova.season, ova.ep_title), ("episode", 0, "Hot Tub"))
        self.assertEqual(items["01. Series/03 - Two Faces.mkv"].subs[0].lang, "en")
        self.assertEqual([l for l, _ in leftovers], ["Torrent Description (READ Me).txt"])

    def test_scene_names(self):
        cases = {
            "The.Office.US.S02E03.720p.WEB-DL.x264-GRP.mkv": ("episode", "The Office US", 2, 3),
            "Show.Name.S01E01E02.1080p.mkv": ("episode", "Show Name", 1, 1),
            "[SubsPlease] Frieren - 12 (1080p) [ABCD].mkv": ("episode", "Frieren", 1, 12),
            "Mr. Robot (2015) Season 1/Mr.Robot.1x05.mkv": ("episode", "Mr Robot", 1, 5),
            "Inception.2010.1080p.BluRay.x264.mkv": ("movie", "Inception", None, None),
        }
        for name, (kind, title, season, ep) in cases.items():
            it = build_items("Root", [(name, 2_000_000_000)])[0][0]
            self.assertEqual((it.kind, it.title, it.season, it.episode), (kind, title, season, ep), name)
        double = build_items("Root", [("Show.Name.S01E01E02.1080p.mkv", 1)])[0][0]
        self.assertEqual(double.episode_end, 2)

    def test_season_tokens_are_not_part_of_the_title(self):
        self.assertEqual(clean_title("Cautious Hero S01 1080p Dual Audio BDRip 10 bits AAC x265-EMBER")[0], "Cautious Hero")
        self.assertEqual(clean_title("The Eminence in Shadow (S1+S2) [BD] [AV1]")[0], "The Eminence in Shadow")
        self.assertEqual(clean_title("Some Show Season 2 720p")[0], "Some Show")
        self.assertEqual(clean_title("Some Show Seasons 1-3")[0], "Some Show")
        self.assertEqual(clean_title("S1m0ne.2002.1080p")[0], "S1m0ne")
        self.assertEqual(clean_title("Area S4 Documentary")[0], "Area S4 Documentary")
        self.assertEqual(clean_title("Show.S02.1080p.WEB-DL")[0], "Show")
        self.assertEqual(clean_title("Show S01 Complete 720p")[0], "Show")

    def test_marketing_words_only_when_they_are_marketing(self):
        self.assertEqual(clean_title("Wolf Pack 1080p")[0], "Wolf Pack")
        self.assertEqual(clean_title("The Complete Savages")[0], "The Complete Savages")
        self.assertEqual(clean_title("Gundam Series - Anime Pack 720p")[0], "Gundam Series")
        self.assertEqual(clean_title("Some Show - The Complete Series (BD)")[0], "Some Show")

    def test_season_folders_with_years_are_not_shows(self):
        files = ["Seasons/Season 01 (2003-2004)/Pack Show S01E01 Pilot.mkv",
                 "Seasons/Season 01 (2003-2004)/Pack Show SP1 - Special One.mkv",
                 "Seasons/Season 17 (2013-2014)/Pack Show S17E01 Later.mkv",
                 "Seasons/Season 17 (2013-2014)/05 - Numbered.mkv", "Seasons/Season 17 (2013-2014)/06 - Numbered Too.mkv"]
        items = {i.src: i for i in build_items("Pack Show", [(f, 10 ** 9) for f in files])[0]}
        self.assertEqual({i.title for i in items.values()}, {"Pack Show"})
        eps = [i for i in items.values() if i.kind == "episode" and i.season]
        self.assertEqual({i.year for i in eps}, {None})   # a season's year doesn't date the show
        self.assertEqual(items[files[2]].season, 17)

    def test_show_year_is_when_it_began(self):
        files = ["Show (2001)/Season 2/Show S02E01.mkv", "Show (2003)/Season 3/Show S03E01.mkv"]
        items = build_items("Pack", [(f, 10 ** 9) for f in files])[0]
        self.assertEqual({(i.title, i.year) for i in items}, {("Show", 2001)})

    def test_samples_and_part_files(self):
        items, _, incomplete = build_items("Movie.2020", [("Movie.2020.mkv", 4_000_000_000),
                                                          ("Sample/movie-sample.mkv", 50_000_000),
                                                          ("extra.nfo.part", 10)])
        kinds = {i.src: i.kind for i in items}
        self.assertEqual(kinds["Sample/movie-sample.mkv"], "skip")
        self.assertEqual(incomplete, ["extra.nfo.part"])


class TestProviders(unittest.TestCase):
    def test_query_variants(self):
        v = query_variants("Space Drill Saga The Movie - 02 (Lagann-hen)")
        self.assertIn("Space Drill Saga Lagann-hen", v)
        self.assertIn("Space Drill Saga The Movie", v)

    def test_scoring_tells_sequels_apart(self):
        q = query_variants("Tengen Toppa Gurren Lagann The Movie - 02 (Lagann-hen)")
        gurren = Match("AniList", "1", "movie", "Gurren Lagann The Movie: Childhood's End", 2008,
                       alt_titles=["Tengen Toppa Gurren Lagann: Gurren-hen"])
        lagann = Match("AniList", "2", "movie", "Gurren Lagann The Movie: The Lights in the Sky are Stars", 2009,
                       alt_titles=["Tengen Toppa Gurren Lagann: Lagann-hen"])
        self.assertGreater(score_match(lagann, q, None, "movie"), score_match(gurren, q, None, "movie"))

    def test_wrong_category_penalised(self):
        q = query_variants("Ninja Scroll")
        show = Match("AniList", "1", "show", "Ninja Scroll: The Series", 2003)
        movie = Match("AniList", "2", "movie", "Ninja Scroll", 1993)
        self.assertGreater(score_match(movie, q, 1993, "movie"), score_match(show, q, 1993, "movie"))

    def test_anime_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "anime-list-full.json").write_text(json.dumps([
                {"type": "TV", "anilist_id": 2001, "mal_id": 2001, "tvdb_id": 80096,
                 "themoviedb_id": {"tv": 21729}, "imdb_id": ["tt0948103"], "season": {"tvdb": 1}},
                {"type": "MOVIE", "anilist_id": 4107, "tvdb_id": 80096, "themoviedb_id": {"tv": 21729},
                 "imdb_id": ["tt1288461"], "season": {"tvdb": 0}}]))
            m = AnimeMap(Http(None), tmp)
            self.assertEqual(m.lookup(anilist=2001)["ids"], {"tvdb": 80096, "tmdb": 21729, "imdb": "tt0948103"})
            movie = m.lookup(mal=None, anilist=4107)["ids"]
            self.assertNotIn("tmdb", movie)  # a TV tmdb id must not be used for a movie
            self.assertEqual(naming.id_tag("movie", movie), " {imdb-tt1288461}")

    def test_a_listed_tmdb_id_is_unwrapped(self):
        # Gintama: THE VERY FINAL came out as '{tmdb-[732203]}' (7 Nov 2026): the map had {"movie": [732203]}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "anime-list-full.json").write_text(json.dumps([
                {"type": "MOVIE", "anilist_id": 114129, "mal_id": 39486, "themoviedb_id": {"movie": [732203]},
                 "imdb_id": "tt10766468"},
                {"type": "TV", "anilist_id": 918, "themoviedb_id": [57041]}]))
            m = AnimeMap(Http(None), tmp)
            movie = m.lookup(anilist=114129)["ids"]
            self.assertEqual(movie["tmdb"], 732203)
            self.assertEqual(naming.id_tag("movie", movie), " {tmdb-732203}")
            self.assertEqual(m.lookup(anilist=918)["ids"]["tmdb"], 57041)
        self.assertEqual(naming.id_tag("movie", {"tmdb": [732203]}), " {tmdb-732203}")   # ids saved before the fix

    def test_parallel_lookups_wait_for_the_map(self):
        # plexsort matches titles in parallel; lookups made while the map was loading got no ids (7 Nov 2026)
        import threading
        from unittest import mock
        from plexsort import providers
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "anime-list-full.json").write_text(json.dumps([
                {"type": "MOVIE", "anilist_id": 7472, "mal_id": 7472, "imdb_id": "tt1636780"}]))
            m = AnimeMap(Http(None), tmp)
            real_loads = json.loads
            def slow_loads(*a, **k):
                time.sleep(0.3)
                return real_loads(*a, **k)
            results = []
            with mock.patch.object(providers.json, "loads", slow_loads):
                threads = [threading.Thread(target=lambda: results.append(m.lookup(anilist=7472))) for _ in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual([r.get("ids", {}).get("imdb") for r in results], ["tt1636780"] * 6)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.download_dir = os.path.join(t, "torrents", "complete")
        os.makedirs(self.download_dir)
        self.cfg = {"movies_dir": os.path.join(t, "media", "Movies"), "tv_dir": os.path.join(t, "media", "TV Shows"),
                    "database": os.path.join(t, "db", "library.db"), "tmdb_api_key": "", "id_tags": True,
                    "remove_torrent": True, "delete_leftovers": True, "holding_dir": os.path.join(t, "holding"),
                    "online": False, "inbox_dirs": ""}
        self.db = DB(self.cfg["database"])
        self.root = make_tree(self.download_dir, PACK, PACK_FILES)
        self.daemon = FakeDaemon([make_torrent(7, PACK, [(f, 100) for f in PACK_FILES], done=1.0, status=6,
                                               download_dir=self.download_dir)]).__enter__()
        self.daemon.torrents[0]["magnetLink"] = "magnet:?xt=urn:btih:abc"
        self.client = Client(self.daemon.url)

    def tearDown(self):
        self.daemon.__exit__(None, None, None)
        self.db.close()
        self.tmp.cleanup()

    def verified_plan(self):
        sources, downloading, err = engine.list_sources(self.cfg, self.client)
        self.assertEqual((len(sources), downloading, err), (1, 0, ""))
        plan = engine.make_plan(sources[0])
        for g in plan.groups():
            if g.category == "show":
                apply_match(g.items, Match("AniList", "1", "show", "Space Drill: Saga", 2007, {"tvdb": 999}))
            else:
                apply_match(g.items, Match("AniList", "2", "movie", "Space Drill Movie", 2008, {"imdb": "tt42"}))
        return plan


class TestEngine(EngineCase):
    def test_plan_destinations(self):
        plan = self.verified_plan()
        check = engine.check_plan(plan, self.cfg)
        self.assertTrue(check.ok, check.errors)
        dests = sorted(os.path.relpath(d, os.path.join(self.tmp.name, "media")) for d in check.dests.values())
        self.assertEqual(dests, [
            "Movies/Space Drill Movie (2008) {imdb-tt42}/Space Drill Movie (2008).mkv",
            "TV Shows/Space Drill - Saga (2007) {tvdb-999}/Other/NCOP 01.mkv",
            "TV Shows/Space Drill - Saga (2007) {tvdb-999}/Season 00/Space Drill - Saga (2007) - s00e06 - Hot Tub.mkv",
            "TV Shows/Space Drill - Saga (2007) {tvdb-999}/Season 01/Space Drill - Saga (2007) - s01e01 - Bust Through.mkv",
            "TV Shows/Space Drill - Saga (2007) {tvdb-999}/Season 01/Space Drill - Saga (2007) - s01e02 - Pilot That Thing.mkv",
            "TV Shows/Space Drill - Saga (2007) {tvdb-999}/Season 01/Space Drill - Saga (2007) - s01e03 - Two Faces.mkv",
        ])

    def test_conflicts_block_apply(self):
        plan = self.verified_plan()
        check = engine.check_plan(plan, self.cfg)
        existing = next(d for d in check.dests.values() if d.endswith(".mkv") and "Movies" in d)
        os.makedirs(os.path.dirname(existing))
        Path(existing).write_text("already here")
        check = engine.check_plan(plan, self.cfg)
        self.assertFalse(check.ok)
        with self.assertRaises(engine.SortError):
            engine.apply_plan(plan, self.cfg, self.db, self.client)
        self.assertTrue(os.path.exists(os.path.join(self.root, "01. Series/01 - Bust Through.mkv")))

    def test_apply_undo_retitle_scan(self):
        plan = self.verified_plan()
        result = engine.apply_plan(plan, self.cfg, self.db, self.client)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.moved, 7)  # 6 videos + 1 subtitle
        self.assertTrue(result.torrent_removed)
        self.assertEqual(self.daemon.torrents, [])
        self.assertFalse([m for m, a in self.daemon.calls if m == "torrent-remove" and a["delete-local-data"]])
        self.assertFalse(os.path.exists(self.root), "torrent folder should be cleaned up")
        show_dir = os.path.join(self.cfg["tv_dir"], "Space Drill - Saga (2007) {tvdb-999}")
        sub = os.path.join(show_dir, "Season 01", "Space Drill - Saga (2007) - s01e03 - Two Faces.en.srt")
        self.assertTrue(os.path.exists(sub))
        titles = self.db.titles()
        self.assertEqual(sorted((t["category"], t["episodes"] or 0) for t in titles), [("movie", 0), ("show", 4)])

        # fix a title: folder and files are renamed
        show = next(t for t in titles if t["category"] == "show")
        moved, errors = engine.retitle(show["id"], "Space Drill Saga", 2007, {"tvdb": 1000}, "manual", self.cfg, self.db)
        self.assertEqual(errors, [])
        new_dir = os.path.join(self.cfg["tv_dir"], "Space Drill Saga (2007) {tvdb-1000}")
        self.assertTrue(os.path.exists(os.path.join(new_dir, "Season 01", "Space Drill Saga (2007) - s01e03 - Two Faces.en.srt")))
        self.assertFalse(os.path.exists(show_dir))

        # scanning finds nothing new, and notices deletions
        self.assertEqual(engine.scan_library(self.cfg, self.db), (0, 0))
        os.remove(os.path.join(new_dir, "Other", "NCOP 01.mkv"))
        self.assertEqual(engine.scan_library(self.cfg, self.db), (0, 1))
        Path(new_dir, "Season 01", "Space Drill Saga (2007) - s01e09 - New.mkv").write_text("x")
        self.assertEqual(engine.scan_library(self.cfg, self.db)[0], 1)

        # undo puts the remaining files back and re-adds the torrent
        restored, errors = engine.undo_operation(result.op_id, self.cfg, self.db, self.client, readd_torrent=True)
        self.assertEqual(restored, 8)     # six sorted, plus two cleared leftovers
        self.assertTrue(any("missing" in e for e in errors))
        self.assertTrue(os.path.exists(os.path.join(self.root, "01. Series/03 - Two Faces.en.srt")))
        self.assertEqual(len(self.daemon.torrents), 1)

    def test_keep_torrent_and_leftovers(self):
        plan = self.verified_plan()
        engine.apply_plan(plan, self.cfg, self.db, self.client, remove_torrent=False, delete_leftovers=False)
        self.assertEqual(len(self.daemon.torrents), 1)
        self.assertTrue(os.path.exists(os.path.join(self.root, "Torrent Description (READ Me).txt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "sample.mkv")))


class TestHoldingFolder(EngineCase):
    """Cleared files move aside for 30 days, and only files known to be junk or skipped are cleared."""

    TXT = "Torrent Description (READ Me).txt"

    def apply(self, **kw):
        return engine.apply_plan(self.verified_plan(), self.cfg, self.db, self.client, **kw)

    def test_junk_and_samples_move_to_the_holding_folder(self):
        res = self.apply()
        hold = os.path.join(self.cfg["holding_dir"], str(res.op_id))
        self.assertTrue(os.path.exists(os.path.join(hold, self.TXT)))
        self.assertTrue(os.path.exists(os.path.join(hold, "sample.mkv")))
        self.assertEqual(res.held, 2)
        self.assertFalse(os.path.exists(self.root))

    def test_files_that_are_not_known_junk_stay_in_the_source(self):
        for name in ("Digital Booklet.pdf", "king.ico", "data.xyz"):
            Path(self.root, name).write_bytes(b"keep me")
        plan = self.verified_plan()
        self.assertEqual(sorted(engine.leftovers_kept(plan)), ["Digital Booklet.pdf", "data.xyz", "king.ico"])
        engine.apply_plan(plan, self.cfg, self.db, self.client)
        for name in ("Digital Booklet.pdf", "king.ico", "data.xyz"):
            self.assertEqual(Path(self.root, name).read_bytes(), b"keep me")
        self.assertFalse(os.path.exists(os.path.join(self.root, self.TXT)))

    def test_undo_puts_them_back(self):
        res = self.apply()
        restored, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])
        self.assertTrue(os.path.exists(os.path.join(self.root, self.TXT)))
        self.assertTrue(os.path.exists(os.path.join(self.root, "sample.mkv")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg["holding_dir"], str(res.op_id))))

    def test_undo_after_they_expired_restores_the_rest(self):
        res = self.apply()
        shutil.rmtree(os.path.join(self.cfg["holding_dir"], str(res.op_id)))
        _, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])
        self.assertIn("no longer held", self.db.operation(res.op_id)["message"])
        self.assertEqual(self.db.operation(res.op_id)["status"], "undone")

    def test_undoing_twice_does_not_move_them_again(self):
        res = self.apply()
        engine.undo_operation(res.op_id, self.cfg, self.db)
        _, errors = engine.undo_operation(res.op_id, self.cfg, self.db)
        self.assertEqual(errors, [])

    def test_if_a_file_cannot_be_held_it_stays(self):
        Path(self.cfg["holding_dir"]).write_bytes(b"a file, so no folder can go here")
        res = self.apply()
        self.assertTrue(os.path.exists(os.path.join(self.root, self.TXT)))
        self.assertTrue(any(self.TXT in e for e in res.errors), res.errors)

    def test_nothing_is_held_when_leftovers_are_kept(self):
        res = self.apply(delete_leftovers=False)
        self.assertEqual(res.held, 0)
        self.assertFalse(os.path.exists(self.cfg["holding_dir"]))

    def test_sorting_clears_expired_folders_and_nothing_else(self):
        hold = Path(self.cfg["holding_dir"])
        for name in ("3", "my-notes"):
            (hold / name).mkdir(parents=True)
            os.utime(hold / name, (1, 1))
        self.apply()
        self.assertFalse((hold / "3").exists())
        self.assertTrue((hold / "my-notes").exists())


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestTUISmoke(EngineCase):
    def setUp(self):
        super().setUp()
        cfg_path = os.path.join(self.tmp.name, "config.toml")
        Path(cfg_path).write_text("\n".join(
            f'{k} = {json.dumps(v) if not isinstance(v, bool) else str(v).lower()}' for k, v in self.cfg.items()) + "\n")
        self.session = f"plexsort-test-{os.getpid()}"
        cmd = (f"PLEXSORT_CONFIG='{cfg_path}' TRCTL_CONFIG=/nonexistent exec {sys.executable} "
               f"'{ROOT / 'bin' / 'plexsort'}' --url {self.daemon.url}")
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session, "-x", "160", "-y", "40", "bash", "-c", cmd],
                       check=True)

    def tearDown(self):
        subprocess.run(["tmux", "kill-session", "-t", self.session], capture_output=True)
        super().tearDown()

    def keys(self, *keys):
        for k in keys:
            subprocess.run(["tmux", "send-keys", "-t", self.session, k], check=True)
            time.sleep(0.2)

    def screen(self):
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", self.session], capture_output=True, text=True).stdout

    def wait_for(self, text, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if text in (s := self.screen()):
                return s
            time.sleep(0.1)
        self.fail(f"{text!r} never appeared:\n{self.screen()}")

    def test_skip_keeps_cursor_and_shift_moves_five(self):
        self.wait_for("Space Drill Saga - The Complete Series")
        self.keys("Enter")
        self.wait_for("not verified")
        self.keys("j")                                   # first episode
        self.wait_for("01. Series/01 - Bust Through.mkv  ·")
        self.keys("Space")                               # skip it: it moves to "Skipped", cursor stays on this row
        s = self.wait_for("01. Series/02 - Pilot That Thing.mkv  ·")
        self.assertRegex(s, r"skip\s+01\. Series/01 - Bust Through\.mkv\s+\(not moved\)")
        self.keys("S-Down")                              # 5 rows down: past 3 files + title row -> the movie file
        self.wait_for("(CBB) Space Drill Saga The Movie - 01 (Gurren-hen).mkv  ·")
        self.keys("S-Up", "J", "K")
        self.wait_for("01. Series/02 - Pilot That Thing.mkv  ·")

    def test_review_edit_apply_undo(self):
        self.wait_for("Space Drill Saga - The Complete Series")
        self.keys("Enter")
        s = self.wait_for("not verified")
        self.assertIn("s01e02", s)
        # cursor starts on the show's title row: give it ids by hand
        self.keys("e")
        self.wait_for("Applies to every file")
        self.keys("Down", "C-u", "Space Drill Saga", "Down", "C-u", "2007", "Down", "999", "Enter")
        self.wait_for("✓ manual")
        # the movie title is the last group: jump there via its file and set ids
        for _ in range(6):  # title row, 5 files, then the movie's title row
            self.keys("j")
        self.keys("e")
        self.wait_for("Applies to every file")
        self.keys("Down", "C-u", "Space Drill Movie", "Down", "C-u", "2008", "Down", "Down", "Down", "tt42", "Enter")
        self.wait_for("ready - press a")
        self.keys("a")
        self.wait_for("Sort into Plex library")
        self.keys("Enter")
        self.wait_for("Sorted 7 file(s)")
        self.assertTrue(os.path.isdir(os.path.join(self.cfg["tv_dir"], "Space Drill Saga (2007) {tvdb-999}")))
        self.assertEqual(self.daemon.torrents, [])
        s = self.wait_for("Space Drill Movie (2008)")
        self.assertIn("4 ep · 2 seasons", s)
        # undo from History
        self.keys("3")
        self.wait_for("applied")
        self.keys("u")
        self.wait_for("Move the files back")
        self.keys("t")
        self.wait_for("re-added the torrent")
        self.assertTrue(os.path.exists(os.path.join(self.root, "01. Series/01 - Bust Through.mkv")))
        self.keys("q")


class TestChecksumsInNames(unittest.TestCase):
    """A release's '[732E889F]' checksum is hex, so it can hold 'E889', which isn't episode 889. Names are from a real torrent."""

    ROOT = "[RH] Danshi Koukousei no Nichijou [BDRip] [Hi10] [1080p]"
    SHOW = "Danshi Koukousei no Nichijou"

    def item(self, tail):
        return build_items(self.ROOT, [(f"[RH] {self.SHOW} - {tail}.mkv", 500_000_000)])[0][0]

    def test_the_episode_number_is_never_read_from_a_checksum(self):
        table = {"01 [1396D8EA]": 1, "03 [732E889F]": 3, "04 [0C940839]": 4, "05 [E4502BC2]": 5, "08 [70E2F617]": 8, "12 [D4351377]": 12}
        for tail, episode in table.items():
            with self.subTest(tail):
                it = self.item(tail)
                self.assertEqual((it.kind, it.title, it.season, it.episode, it.ep_title), ("episode", self.SHOW, 1, episode, ""))

    def test_other_checksum_forms(self):
        for name, episode in (("Show - 03 (732E889F).mkv", 3), ("Show.E05.[70E2F617].mkv", 5), ("Show S01E06 [E4502BC2].mkv", 6)):
            with self.subTest(name):
                self.assertEqual(build_items("Show", [(name, 500_000_000)])[0][0].episode, episode)

    def test_a_number_glued_to_other_characters_is_not_an_episode(self):
        self.assertEqual(build_items("Pack", [("Some Film ab12E45.mkv", 500_000_000)])[0][0].kind, "movie")

    def test_an_episode_word_is_still_an_episode(self):
        for name in ("Show E07.mkv", "Show Ep 07.mkv", "Show - Episode 7.mkv"):
            with self.subTest(name):
                self.assertEqual(build_items("Pack", [(name, 500_000_000)])[0][0].episode, 7)

    def test_short_specials_are_extras_of_the_show(self):
        for tail, name in (("S1 [23C2AFFF]", "S1"), ("S2 [3395E4AE]", "S2"), ("S4 [C67B1E0D]", "S4"), ("S5 [77A53E65]", "S5"), ("S7 [0348AD6E]", "S7")):
            with self.subTest(tail):
                it = self.item(tail)
                self.assertEqual((it.kind, it.category, it.title, it.extra_name, it.season, it.episode),
                                 ("extra", "show", self.SHOW, name, None, None))

    def test_creditless_videos_lose_the_show_name(self):
        for tail, name in (("NCED [1014B25C]", "NCED"), ("NCOP1 [8B38F694]", "NCOP1")):
            with self.subTest(tail):
                it = self.item(tail)
                self.assertEqual((it.kind, it.title, it.extra_name), ("extra", self.SHOW, name))

    def test_a_season_in_a_real_episode_name_still_counts(self):
        it = build_items("Show", [("Show S02E03 [70E2F617].mkv", 500_000_000)])[0][0]
        self.assertEqual((it.season, it.episode), (2, 3))

    def test_the_whole_torrent_is_one_show(self):
        names = [f"[RH] {self.SHOW} - {n:02d} [{h}].mkv" for n, h in enumerate(
            ["1396D8EA", "6281C9C0", "732E889F", "0C940839", "E4502BC2", "C0AA3474", "DB03B011", "70E2F617"], start=1)]
        names += [f"[RH] {self.SHOW} - S{n} [{h}].mkv" for n, h in enumerate(["23C2AFFF", "3395E4AE", "AA9C3B74"], start=1)]
        names += [f"[RH] {self.SHOW} - NCOP1 [8B38F694].mkv"]
        plan = Plan(self.ROOT, "/x", *build_items(self.ROOT, [(n, 500_000_000) for n in names])[:2])
        self.assertEqual([(g.category, g.title, len(g.items)) for g in plan.groups()], [("show", self.SHOW, 12)])
        self.assertEqual(sorted(i.episode for i in plan.items if i.kind == "episode"), list(range(1, 9)))


class TestCodedSpecials(unittest.TestCase):
    """A fan collection (GameCenter CX): one show, with specials coded B8, NC11, SP1... The names are the torrent's own."""

    ROOT = "GameCenter CX"
    S = "Seasons/Season {}/GameCenter CX "

    def item(self, rel):
        return build_items(self.ROOT, [(rel, 1)])[0][0]

    def test_coded_files_are_extras_of_the_show(self):
        table = {"DVD Bonus Episodes/GameCenter CX - B8 - City Connection [XerdoSpecter].mkv": "B8 - City Connection",
                 "DVD Bonus Episodes/GameCenter CX - B15-1 - Resident Evil 2 (Part 1) [LocationScouts].mkv": "B15-1 - Resident Evil 2 (Part 1)",
                 "DVD Bonus Episodes/GameCenter CX - B3SF - Ring Ring Tactics Compilation.avi": "B3SF - Ring Ring Tactics Compilation",
                 "DVD Bonus Episodes/GameCenter CX - BUSA - GCCX in the USA Bonus Challenge.mkv": "BUSA - GCCX in the USA Bonus Challenge",
                 "DVD Bonus Episodes/GameCenter CX - RE1 - Legend of the Mystical Ninja (Revenge)   [VR-GCCX].avi": "RE1 - Legend of the Mystical Ninja (Revenge)",
                 "Nintendo Channel/GameCenter CX NC11 - Excitebike 3D.avi": "NC11 - Excitebike 3D",
                 "Nintendo Channel/GameCenter CX NC04 - Downtown Special.avi": "NC04 - Downtown Special",
                 "Nintendo Channel/GameCenter CX NC09 - Super Metroid (Part 2).avi": "NC09 - Super Metroid (Part 2)",
                 self.S.format("01 (2003-2004)") + "SP1 - Nintendo - Super Mario Brothers 2 (Part 2) [SA-GCCX].avi": "SP1 - Nintendo - Super Mario Brothers 2 (Part 2)",
                 "Seasons/Season 11 (2009)/GameCenter CX - LE3E1 - Lemmings Live The Last Continue  [SA-GCCX].avi": "LE3E1 - Lemmings Live The Last Continue",
                 self.S.format("19 (2015-2016)") + "S19 - LE9P - Super Mario Maker Live Challenge.mkv": "LE9P - Super Mario Maker Live Challenge",
                 "Seasons/Season 20 (2016)/GameCenter CX - YE6E - 15 New Year's Eve 2016 - Kiki Kaikai Revenge.mkv": "YE6E - 15 New Year's Eve 2016 - Kiki Kaikai Revenge",
                 "Seasons/Season 10 (2008-2009)/GameCenter CX - SP2D - Southernmost Game Travelogue (2008)  [SA-GCCX].avi": "SP2D - Southernmost Game Travelogue"}
        for rel, name in table.items():
            with self.subTest(rel):
                it = self.item(rel)
                self.assertEqual((it.kind, it.category, it.title, it.extra_name), ("extra", "show", self.ROOT, name))

    def test_extras_in_a_folder_belong_to_the_show_not_the_folder(self):
        it = self.item("Misc - Extras/GameCenter CX - Genki TV Interview.avi")
        self.assertEqual((it.kind, it.title, it.extra_name), ("extra", self.ROOT, "Genki TV Interview"))

    def test_regular_episodes_keep_their_numbers(self):
        it = self.item(self.S.format("07 (2007)") + "S07E09 #53 - Clocktower (challenge).mkv")
        self.assertEqual((it.kind, it.season, it.episode), ("episode", 7, 9))

    def test_it_only_applies_under_the_torrents_own_name(self):
        for root, rel, kind in (("Root", "Other Show - B8 - Something.mkv", "movie"), ("Show Pack", "Show Pack - Pilot.mkv", "movie"),
                                ("Show Pack", "Show Pack - S01E02 - Pilot.mkv", "episode")):
            with self.subTest(rel):
                self.assertEqual(build_items(root, [(rel, 1)])[0][0].kind, kind)

    def test_the_collection_is_one_show_and_nothing_shares_e01_after_matching(self):
        names = [f"Nintendo Channel/GameCenter CX NC{n:02d} - Game {n}.avi" for n in range(1, 8)]
        names += ["Misc - Extras/GameCenter CX - Genki TV Interview.avi", self.S.format("01 (2003-2004)") + "S01E01 #1 - First.mkv",
                  self.S.format("01 (2003-2004)") + "S01E02 #2 - Second.mkv", "DVD Bonus Episodes/GameCenter CX - B8 - City Connection.mkv"]
        plan = Plan(self.ROOT, "/x", *build_items(self.ROOT, [(n, 1) for n in names])[:2])
        groups = plan.groups()
        self.assertEqual([(g.title, len(g.items)) for g in groups], [(self.ROOT, 11)])
        apply_match(groups[0].items, Match("TVDB", "1", "show", self.ROOT, 2003), None)
        numbered = [(i.season, i.episode) for i in plan.items if i.kind == "episode"]
        self.assertEqual(sorted(numbered), [(1, 1), (1, 2)])


class TestMatchingAShow(unittest.TestCase):
    SHOW = Match("TVDB", "1", "show", "Some Show", 2003)

    def test_a_movie_named_show_plus_more_becomes_an_extra_not_episode_one(self):
        it = build_items("Pack", [("Some Show Holiday Bash.mkv", 1)])[0][0]
        it.kind, it.title = "movie", "Some Show Holiday Bash"
        apply_match([it], self.SHOW, None)
        self.assertEqual((it.kind, it.extra_name, it.season, it.episode), ("extra", "Some Show Holiday Bash", None, None))

    def test_a_movie_named_as_the_show_is_still_an_episode(self):
        it = build_items("Pack", [("Some Show.mkv", 1)])[0][0]
        it.kind, it.title = "movie", "Some Show"
        apply_match([it], self.SHOW, None)
        self.assertEqual((it.kind, it.season, it.episode), ("episode", 1, 1))

    def test_a_movie_under_another_title_is_still_an_episode(self):
        it = build_items("Pack", [("Fighting Spirit.mkv", 1)])[0][0]
        it.kind, it.title = "movie", "Fighting Spirit"
        apply_match([it], self.SHOW, None)
        self.assertEqual((it.kind, it.season, it.episode), ("episode", 1, 1))

    def test_alternate_cuts_of_one_number_keep_their_own_titles(self):
        rels = ["S07E09 #53 - Segments.avi", "S07E09 #53 - Clocktower (challenge).mkv", "S07E10 #54 - Lone.mkv"]
        items = build_items("Pack", [(f"Some Show {r}", 1) for r in rels])[0]

        class Listing:
            def episodes(self, match):
                return [Episode(7, 9, "Online Nine"), Episode(7, 10, "Online Ten")]
        apply_match(items, self.SHOW, Listing())
        titles = sorted(i.ep_title for i in items)
        self.assertEqual(titles, ["#53 - Clocktower (challenge)", "#53 - Segments", "Online Ten"])


class TestYearsInNames(unittest.TestCase):
    """Finding 8: a release year goes, a year that's part of the name stays."""

    def test_names(self):
        table = {"Pilot 2019 1080p": "Pilot", "2019 1080p": "", "Making Of 2019 1080p": "Making Of", "Making Of (2019) - 1080p": "Making Of",
                 "Making Of (2019)": "Making Of", "Making Of (2018-2019)": "Making Of", "Making Of (2018-19) 720p": "Making Of",
                 "Making Of - (2019)": "Making Of", "At Anime Expo 2018 Interview With Staff": "At Anime Expo 2018 Interview With Staff",
                 "At Anime Expo 2018": "At Anime Expo 2018", "Blade Runner 2049 Featurette 1080p": "Blade Runner 2049 Featurette"}
        for raw, want in table.items():
            with self.subTest(raw):
                self.assertEqual(clean_title(raw, cut_marketing=False, recase=False, keep_years=True)[0], want)

    def test_movie_titles_still_end_at_the_year(self):
        self.assertEqual(clean_title("Blade Runner 2049 1080p"), ("Blade Runner", 2049))


if __name__ == "__main__":
    unittest.main()


class TestTrailingEpisodeNumbers(unittest.TestCase):
    """'Show 003.mkv': a bare number at the end of the name, the way some BD anime packs number episodes.
    Found 7 Nov 2026: a Gintama pack went into Movies as 368 one-episode 'movies'."""
    ROOT = "Gintama [BD] [1080p] [HEVC]"

    def items(self, files):
        items, _, _ = build_items(self.ROOT, [(f, 2_000_000_000) for f in files])
        return {i.src: i for i in items}

    def test_a_run_of_padded_numbers_is_episodes(self):
        files = ["[2006] Gintama/Gintama 001-002.mkv"] + [f"[2006] Gintama/Gintama {n:03d}.mkv" for n in range(3, 9)]
        files += [f"[2011] Gintama'/Gintama {n}.mkv" for n in range(202, 210)]
        files += ["[Movie] [2010] Gintama Shinyaku Benizakura-hen/[Movie] [2010] Gintama Shinyaku Benizakura-hen.mkv"]
        items = self.items(files)
        first = items["[2006] Gintama/Gintama 001-002.mkv"]
        self.assertEqual((first.kind, first.category, first.title, first.episode, first.episode_end),
                         ("episode", "show", "Gintama", 1, 2))
        self.assertTrue(first.absolute)
        later = items["[2011] Gintama'/Gintama 203.mkv"]
        self.assertEqual((later.kind, later.title, later.episode), ("episode", "Gintama", 203))
        self.assertTrue(later.absolute)
        # one show across the season folders, so the absolute numbers map onto its seasons
        shows = {(i.title, i.year) for i in items.values() if i.kind == "episode"}
        self.assertEqual(shows, {("Gintama", 2006)})
        movie = items["[Movie] [2010] Gintama Shinyaku Benizakura-hen/[Movie] [2010] Gintama Shinyaku Benizakura-hen.mkv"]
        self.assertEqual(movie.kind, "movie")

    def test_a_movie_collection_is_not_episodes(self):
        # few files, no zero padding: sequels, not episodes
        items = self.items(["Rocky Collection/Rocky 2.mkv", "Rocky Collection/Rocky 3.mkv", "Rocky Collection/Rocky 4.mkv"])
        self.assertEqual({i.kind for i in items.values()}, {"movie"})
        # a year in the name means a film, however many there are
        films = [f"Pack/Film {n} ({2000 + n}).mkv" for n in range(1, 7)]
        self.assertEqual({i.kind for i in self.items(films).values()}, {"movie"})
        # a single numbered file is never an episode on its own
        self.assertEqual(self.items(["Apollo 13.mkv"])["Apollo 13.mkv"].kind, "movie")
        # a year at the end isn't an episode number
        years = [f"Docs/Report {y}.mkv" for y in range(2010, 2016)]
        self.assertEqual({i.kind for i in self.items(years).values()}, {"movie"})
