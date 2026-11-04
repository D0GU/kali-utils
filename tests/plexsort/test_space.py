"""Multiple library folders, disk-space-aware placement, folder config. Offline, temp folders only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]  # kali-utils root
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "trctl")]  # trctl's fake daemon

from plexsort import config, engine, naming, space  # noqa: E402
from plexsort.db import DB  # noqa: E402
from plexsort.providers import Match, apply_match  # noqa: E402

GB = space.GB


class FakeDrives:
    """Pretend each top-level temp folder is its own drive with a given amount of free space."""

    def __init__(self, base: str, drives: dict[str, int], source_drive: str):
        self.base, self.drives, self.source_drive = base, drives, source_drive

    def drive_of(self, path: str) -> str:
        rel = os.path.relpath(os.path.abspath(path), self.base)
        return rel.split("/")[0]

    def drive_info(self, path):
        d = self.drive_of(path)
        return hash(d) & 0xFFFF, self.drives[d], 2000 * GB

    def source_device(self, path):
        return hash(self.source_drive) & 0xFFFF

    def __enter__(self):
        self.patches = [mock.patch.object(space, "drive_info", self.drive_info),
                        mock.patch.object(space, "source_device", self.source_device)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class PlacementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        for d in ("ssd", "hdd1", "hdd2", "torrents"):
            os.makedirs(f"{t}/{d}")
        self.cfg = {"movies_dirs": [f"{t}/hdd1/Movies", f"{t}/hdd2/Movies"], "tv_dirs": [f"{t}/hdd1/TV", f"{t}/hdd2/TV"],
                    "music_dirs": [f"{t}/hdd1/Music"], "database": f"{t}/ssd/db.sqlite", "id_tags": True,
                    "min_free_gb": 2, "online": False, "download_cover_art": False, "remove_torrent": True,
                    "delete_leftovers": True, "holding_dir": f"{t}/holding", "inbox_dirs": []}
        self.db = DB(self.cfg["database"])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def make_source(self, name: str, files: dict[str, int]) -> engine.Source:
        root = os.path.join(self.tmp.name, "torrents", name)
        for rel, size in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.truncate(size)  # sparse: big logical size, no real disk use
        return engine.source_for_path(root)

    def plan(self, name, files, **drives):
        plan = engine.make_plan(self.make_source(name, files))
        for g in plan.groups():
            apply_match(g.items, Match("test", "1", g.category, g.title, g.year or 2001, {}))
        return plan

    def placements(self, plan, drives, source="torrents"):
        with FakeDrives(self.tmp.name, drives, source):
            check = engine.check_plan(plan, self.cfg)
        return check, {p.label: p for p in check.placements}


class TestPlacement(PlacementCase):
    def test_preferred_folder_when_it_fits(self):
        plan = self.plan("Movie.2001", {"Movie.2001.mkv": 5 * GB})
        check, pl = self.placements(plan, {"hdd1": 100 * GB, "hdd2": 900 * GB, "torrents": 0, "ssd": 0})
        self.assertTrue(check.ok)
        (p,) = pl.values()
        self.assertEqual((p.root, p.reason), (self.cfg["movies_dirs"][0], "preferred folder"))
        self.assertTrue(next(iter(check.dests.values())).startswith(self.cfg["movies_dirs"][0]))

    def test_falls_back_to_drive_with_room(self):
        plan = self.plan("Movie.2001", {"Movie.2001.mkv": 5 * GB})
        check, pl = self.placements(plan, {"hdd1": 6 * GB, "hdd2": 900 * GB, "torrents": 0, "ssd": 0})
        (p,) = pl.values()
        self.assertEqual(p.root, self.cfg["movies_dirs"][1])  # 6 GB free - 2 GB reserve < 5 GB
        self.assertIn("preferred drive too full", p.reason)

    def test_keeps_titles_together(self):
        plan = self.plan("Show.S02", {"Show.S02E01.mkv": GB, "Show.S02E02.mkv": GB})
        g = plan.groups()[0]
        os.makedirs(os.path.join(self.cfg["tv_dirs"][1], naming.title_dir_name(g.items[0], self.cfg)))
        check, pl = self.placements(plan, {"hdd1": 500 * GB, "hdd2": 500 * GB, "torrents": 0, "ssd": 0})
        (p,) = pl.values()
        self.assertEqual((p.root, p.reason), (self.cfg["tv_dirs"][1], "already has this title"))

    def test_same_drive_needs_no_space(self):
        self.cfg["movies_dirs"] = [os.path.join(self.tmp.name, "torrents", "Movies")]
        plan = self.plan("Movie.2001", {"Movie.2001.mkv": 50 * GB})
        check, pl = self.placements(plan, {"torrents": 3 * GB, "hdd1": 0, "hdd2": 0, "ssd": 0})
        (p,) = pl.values()
        self.assertTrue(p.fits)
        self.assertIn("instant move", p.reason)

    def test_nothing_fits_blocks_apply(self):
        plan = self.plan("Movie.2001", {"Movie.2001.mkv": 50 * GB})
        check, pl = self.placements(plan, {"hdd1": 10 * GB, "hdd2": 20 * GB, "torrents": 0, "ssd": 0})
        self.assertFalse(check.ok)
        self.assertIn("no movie folder has room", check.global_errors[0])
        with FakeDrives(self.tmp.name, {"hdd1": 10 * GB, "hdd2": 20 * GB, "torrents": 0, "ssd": 0}, "torrents"):
            with self.assertRaises(engine.SortError):
                engine.apply_plan(plan, self.cfg, self.db)

    def test_space_is_reserved_across_titles(self):
        plan = self.plan("Pack", {"Movie.A.2001/Movie.A.2001.mkv": 30 * GB, "Movie.B.2002/Movie.B.2002.mkv": 20 * GB})
        check, pl = self.placements(plan, {"hdd1": 45 * GB, "hdd2": 500 * GB, "torrents": 0, "ssd": 0})
        roots = sorted((p.need // GB, p.root.split("/")[-2]) for p in pl.values())
        self.assertEqual(roots, [(20, "hdd2"), (30, "hdd1")])  # largest placed first; the second no longer fits hdd1

    def test_manual_choice_and_apply_moves_there(self):
        plan = self.plan("Movie.2001", {"Movie.2001.mkv": 1000})
        for it in plan.items:
            it.root, it.root_manual = self.cfg["movies_dirs"][1], True
        with FakeDrives(self.tmp.name, {"hdd1": 500 * GB, "hdd2": 500 * GB, "torrents": 0, "ssd": 0}, "torrents"):
            result = engine.apply_plan(plan, self.cfg, self.db)
        self.assertEqual(result.errors, [])
        moved = os.listdir(self.cfg["movies_dirs"][1])
        self.assertEqual(len(moved), 1)
        (row,) = self.db.titles()
        self.assertEqual(naming.root_of(row["path"], "movie", self.cfg), self.cfg["movies_dirs"][1])
        # re-titling keeps it on the same drive, and scans/undo find it there
        engine.retitle(row["id"], "Other Movie", 2001, {}, "manual", self.cfg, self.db)
        self.assertTrue(os.path.isdir(os.path.join(self.cfg["movies_dirs"][1], "Other Movie (2001)")))
        self.assertEqual(engine.scan_library(self.cfg, self.db), (0, 0))
        restored, errors = engine.undo_operation(result.op_id, self.cfg, self.db)
        self.assertEqual((restored, errors), (1, []))


class TestConfig(unittest.TestCase):
    def test_legacy_single_folders_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.toml")
            Path(path).write_text('movies_dir = "/a/Movies"\ntv_dir = "/a/TV"\ninbox_dirs = "/x, /y"\n')
            with mock.patch.dict(os.environ, {"PLEXSORT_CONFIG": path}):
                cfg = config.load()
                self.assertEqual((cfg["movies_dirs"], cfg["tv_dirs"], cfg["inbox_dirs"]),
                                 (["/a/Movies"], ["/a/TV"], ["/x", "/y"]))
                cfg["movies_dirs"].append("/b/Movies \"quoted\"")
                config.save(cfg)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                data = tomllib.loads(Path(path).read_text())
                self.assertNotIn("movies_dir", data)
                self.assertEqual(config.load()["movies_dirs"], ["/a/Movies", '/b/Movies "quoted"'])

    def test_library_roots_fallbacks(self):
        self.assertEqual(naming.library_roots("movie", {"movies_dir": "/m"}), ["/m"])
        self.assertEqual(naming.library_roots("music", {"movies_dirs": ["/media/Movies"]}), ["/media/Music"])
        cfg = {"tv_dirs": ["/d1/TV", "/d2/TV"]}
        self.assertEqual(naming.root_of("/d2/TV/Show (2001)/Season 01/x.mkv", "show", cfg), "/d2/TV")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestFolderDialog(unittest.TestCase):
    def test_add_a_second_movies_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.toml")
            Path(cfg_path).write_text(f'movies_dirs = ["{tmp}/d1/Movies"]\ntv_dirs = ["{tmp}/d1/TV"]\n'
                                      f'music_dirs = ["{tmp}/d1/Music"]\ndatabase = "{tmp}/db.sqlite"\nonline = false\n')
            os.makedirs(f"{tmp}/d2/Films")
            session = f"plexsort-folders-{os.getpid()}"
            cmd = (f"PLEXSORT_CONFIG='{cfg_path}' TRCTL_CONFIG=/nonexistent {sys.executable} "
                   f"'{ROOT / 'bin' / 'plexsort'}' --url http://127.0.0.1:1 setup")
            subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "140", "-y", "40", "bash", "-c",
                            cmd + "; sleep 5"], check=True)
            try:
                def screen():
                    return subprocess.run(["tmux", "capture-pane", "-p", "-t", session], capture_output=True,
                                          text=True).stdout

                def wait(text):
                    end = time.time() + 8
                    while time.time() < end:
                        if text in screen():
                            return
                        time.sleep(0.1)
                    self.fail(f"{text!r} not on screen:\n{screen()}")

                def keys(*ks):
                    for k in ks:
                        subprocess.run(["tmux", "send-keys", "-t", session, k], check=True)
                        time.sleep(0.25)
                wait("Library & watched folders")
                keys("j", "j")                      # watched "+ add" -> Movies folder -> Movies "+ add"
                keys("Enter")
                wait("Use this folder")
                keys("g")
                wait("Go to folder")
                keys("C-u", f"{tmp}/d2/Films", "Enter")
                wait(f"{tmp}/d2/Films")
                keys("Enter")                       # ✓ Use this folder
                wait("d2/Films")
                keys("w")
                wait("Saved")
            finally:
                subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
            data = tomllib.loads(Path(cfg_path).read_text())
            self.assertEqual(data["movies_dirs"], [f"{tmp}/d1/Movies", f"{tmp}/d2/Films"])
            self.assertTrue(os.path.isdir(f"{tmp}/d1/TV"))


if __name__ == "__main__":
    unittest.main()
