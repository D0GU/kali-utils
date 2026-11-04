"""kvctl's Kavita client against a fake server."""

from __future__ import annotations

import unittest

from kvctl.client import BOOK_LIBRARY, AuthError, Kavita, KavitaError
from tests.kvctl.helpers import API_KEY, FakeKavita


def library(lid=1, name="Books", folders=("/data/Books",), **extra):
    return {"id": lid, "name": name, "type": BOOK_LIBRARY, "folders": list(folders), **extra}


class TestClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKavita([library()]).__enter__()
        self.addCleanup(self.fake.__exit__)
        self.client = Kavita(self.fake.url, API_KEY)

    def last(self):
        return self.fake.calls[-1]

    def test_health_needs_no_key(self):
        self.assertTrue(Kavita(self.fake.url).healthy())

    def test_missing_and_wrong_keys(self):
        with self.assertRaises(AuthError):
            Kavita(self.fake.url).libraries()
        with self.assertRaises(AuthError):
            Kavita(self.fake.url, "wrong").libraries()

    def test_unreachable_server(self):
        with self.assertRaises(KavitaError) as caught:
            Kavita("http://127.0.0.1:9", API_KEY, timeout=2).libraries()
        self.assertIn("cannot reach", str(caught.exception))

    def test_create_library_body(self):
        self.client.create_library("New", ["/data/New"])
        _, path, _, body = self.last()
        self.assertEqual(path, "/api/Library/create")
        self.assertEqual((body["name"], body["type"], body["folders"], body["fileGroupTypes"]), ("New", 2, ["/data/New"], [2, 3]))

    def test_update_keeps_other_settings(self):
        lib = library(folderWatching=False, includeInSearch=False, libraryFileTypes=[{"fileTypeGroup": 2}],
                      excludePatterns=[{"pattern": "*.tmp"}], defaultLanguage="en")
        self.fake.libraries[0] = lib
        self.client.update_library(lib, name="Renamed")
        body = self.last()[3]
        self.assertEqual((body["id"], body["name"], body["folders"]), (1, "Renamed", ["/data/Books"]))
        self.assertEqual((body["folderWatching"], body["includeInSearch"], body["fileGroupTypes"], body["excludePatterns"], body["defaultLanguage"]),
                         (False, False, [2], ["*.tmp"], "en"))

    def test_scan_parameters(self):
        self.client.scan(1, force=True)
        self.assertEqual(self.last()[2], {"libraryId": "1", "force": "true"})
        self.client.scan_all()
        self.assertEqual(self.last()[1:3], ("/api/Library/scan-all", {"force": "false"}))

    def test_delete(self):
        self.client.delete_library(1)
        self.assertEqual(self.last()[0:3], ("DELETE", "/api/Library/delete", {"libraryId": "1"}))
        self.assertEqual(self.fake.libraries, [])

    def test_library_for_folder_prefers_the_closest_root(self):
        self.fake.libraries.append(library(2, "Inner", ["/data/Books/Inner"]))
        self.assertEqual(self.client.library_for_folder("/data/Books/Inner/x")["id"], 2)
        self.assertEqual(self.client.library_for_folder("/data/Books/Other")["id"], 1)
        self.assertIsNone(self.client.library_for_folder("/elsewhere"))

    def test_series_filter_and_paging(self):
        self.fake.series = [{"id": i, "name": f"S{i}", "libraryId": 1 if i % 2 else 2} for i in range(1, 9)]
        self.assertEqual([s["id"] for s in self.client.all_series(1, size=2)], [1, 3, 5, 7])
        pages = [c[2]["PageNumber"] for c in self.fake.calls if c[1] == "/api/Series/all-v2"]
        self.assertEqual(pages, ["1", "2", "3"])
        statement = self.last()[3]["statements"][0]
        self.assertEqual(statement, {"comparison": 5, "field": 19, "value": "1"})
        self.assertEqual(len(self.client.all_series()), 8)

    def test_http_error_is_reported(self):
        with self.assertRaises(KavitaError) as caught:
            self.client._call("GET", "/api/Nope")
        self.assertIn("404", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
