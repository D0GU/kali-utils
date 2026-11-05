"""The real reader in a real terminal, with a stand-in model that records everything it is sent."""

from __future__ import annotations

import json
import re
import os
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.bookread.test_ebook import ncx, point
from tests.bookread.test_ui import UI

SECRET = "Zarek"
FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 6


class FakeModel(BaseHTTPRequestHandler):
    prompts: list[str] = []
    down = False

    def log_message(self, *args):
        pass

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send([{"is_processing": False}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        FakeModel.prompts.append(user)
        if "take notes" in system:
            lines = []
            if "Anna found the brass key" in user:
                lines.append("Anna | Anna found a key | Anna found the brass key")
            if f"{SECRET} burned the mill" in user:
                lines.append(f"{SECRET} | {SECRET} burned the mill | {SECRET} burned the mill")
            content = "\n".join(lines)
        elif "notes about a book" in system:
            content = json.dumps({"supported": [int(n) for n in re.findall(r"^Note (\d+) ", user, re.M)]})
        elif "response_format" in body:
            content = '{"supported": true}'
        else:
            content = "The key is safe with me [1]."
        self._send({"choices": [{"message": {"content": content}}]})


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class Marginalia(UI):
    def setUp(self):
        super().setUp()
        FakeModel.prompts = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        alpha = "<h1>Alpha</h1><p>Anna found the brass key under the wheel.</p>" + "".join(f"<p>{FILLER}</p>" for _ in range(60))
        beta = f"<h1>Beta</h1><p>{SECRET} burned the mill.</p>" + "".join(f"<p>{FILLER}</p>" for _ in range(60))
        self.book = self.add_book("Mill Story", "Cay Doe", 12, [("a.xhtml", alpha), ("b.xhtml", beta)],
                                  ncx=ncx(point("Alpha", "a.xhtml"), point("Beta", "b.xhtml")))

    def settings(self, text: str) -> None:
        with open(self.ws.config, "a") as f:
            f.write("\n[marginalia]\n" + text)

    def connect(self) -> None:
        self.settings(f'provider = "llamacpp"\nmodel_url = "http://127.0.0.1:{self.server.server_port}/v1"\nmodel_key = "k"\n')

    def turn_pages(self, n: int) -> None:
        for _ in range(n):
            self.keys("Space")

    def leaked(self) -> bool:
        return any(SECRET in p for p in FakeModel.prompts)

    def test_talking_with_someone_you_have_met(self):
        self.connect()
        self.start(self.book)
        self.wait_for("Alpha")
        self.turn_pages(6)
        self.keys("m")
        self.wait_for("MARGINALIA")
        self.wait_for("Anna", 15)
        self.assertNotIn(SECRET, self.screen())
        self.keys("Enter")
        self.wait_for("Enter send")
        self.keys("-l", "where is the key?")
        self.wait_for("where is the key?")
        self.keys("Enter")
        screen = self.wait_for("The key is safe with me", 10)
        self.assertIn("from this page", screen)
        self.assertNotIn("[1]", screen, "the panel shows chapters, not passage numbers")
        self.assertFalse(self.leaked(), "nothing past your place reached the model")

        # The page keeps working beside it, and q types rather than quits while the panel has the keys.
        self.keys("q")
        self.wait_for("> q")
        self.keys("BSpace", "Tab")
        self.wait_for("m back to the panel")
        before = self.stored(self.book)["position"]
        self.keys("Space")
        for _ in range(100):
            if self.stored(self.book)["position"] != before:
                break
            threading.Event().wait(0.02)
        self.assertNotEqual(self.stored(self.book)["position"], before)
        self.keys("m")
        self.wait_for("Enter send")
        self.keys("Escape")
        self.wait_for("Enter talk")
        self.keys("Escape")
        self.wait_gone("MARGINALIA")
        self.keys("q")
        self.wait_exit()

    def test_a_jump_ahead_asks_before_they_learn_anything(self):
        self.connect()
        self.start(self.book)
        self.wait_for("Alpha")
        self.turn_pages(6)
        self.keys("m")
        self.wait_for("Anna", 15)
        self.keys("Tab", "G")
        self.keys("m")
        self.wait_for("jumped ahead")
        threading.Event().wait(6)       # the preparer's wait: it has had its chance to read on
        self.assertFalse(self.leaked(), "a jump alone teaches them nothing")
        self.keys("y")
        self.wait_for(SECRET, 20)
        self.assertTrue(self.leaked())

        # Going back to reread means they know less again.
        self.keys("Tab", "g")
        self.keys("m")
        self.wait_gone(SECRET)
        self.keys("Escape", "q")
        self.wait_exit()

    def test_a_narrow_window_gives_the_panel_all_of_it(self):
        self.connect()
        self.start(self.book, cols=50)
        self.wait_for("Alpha")
        self.keys("m")
        screen = self.wait_for("MARGINALIA")
        self.assertNotIn("│", screen)
        self.keys("Tab")
        self.wait_gone("MARGINALIA")
        self.keys("q")
        self.wait_exit()

    def test_off_by_default_it_says_so_and_reading_carries_on(self):
        self.start(self.book)
        self.wait_for("Alpha")
        self.keys("m")
        self.wait_for("The model is off for now")
        self.keys("Escape")
        self.wait_gone("MARGINALIA")
        self.keys("q")
        self.wait_exit()

    def test_without_a_key_it_says_so_and_reading_carries_on(self):
        self.settings('provider = "claude"\n')
        self.start(self.book)
        self.wait_for("Alpha")
        self.keys("m")
        self.wait_for("No key for the model")
        self.keys("Escape")
        self.wait_gone("MARGINALIA")
        self.keys("q")
        self.wait_exit()

    def test_with_the_model_down_it_says_so_and_reading_carries_on(self):
        self.settings('provider = "llamacpp"\nmodel_url = "http://127.0.0.1:9/v1"\nmodel_key = "k"\n')
        self.start(self.book)
        self.wait_for("Alpha")
        self.turn_pages(6)
        self.keys("m")
        self.wait_for("isn't answering", 15)
        self.keys("Tab", "Space", "Space")
        self.keys("m", "Escape", "q")
        self.wait_exit()

    def test_turned_off_it_leaves_no_trace(self):
        self.settings("enabled = false\n")
        self.start(self.book)
        self.wait_for("Alpha")
        self.keys("m")
        threading.Event().wait(0.5)
        self.assertNotIn("MARGINALIA", self.screen())
        self.keys("q")
        self.wait_exit()
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.ws.database), "marginalia.db")))

    def test_unused_it_writes_nothing(self):
        self.start(self.book)
        self.wait_for("Alpha")
        self.turn_pages(2)
        self.keys("q")
        self.wait_exit()
        self.assertFalse(self.leaked())
        self.assertEqual(FakeModel.prompts, [])


del UI      # or unittest would run all of its tests over again from here


if __name__ == "__main__":
    unittest.main()
