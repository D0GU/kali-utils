"""Marginalia's memory, lock and answers, with a model that does as it's told. Nothing here needs a real one."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from bookread import ebook
from bookread.marginalia import ask as ask_module
from bookread.marginalia.ask import NOT_YET, UNSURE, ask, build_prompt, tokens
from bookread.marginalia.memory import Memory, Passage, normal
from bookread.marginalia.model import BadReply, CLAUDE_MIN_TOKENS, ClaudeModel, Model, ModelError
from bookread.marginalia.panel import model_from
from bookread.marginalia.prepare import MAX_ERROR_WAIT, PASSAGE_WORDS, error_wait, read_notes, split_passages
from tests.bookread.helpers import make_book

BOOK = "book-key"
EARLY = "Anna found the brass key under the mill wheel and hid it from Tomas."
LATE = "Tomas betrayed Anna and burned the mill to the ground that winter."


class FakeModel:
    def __init__(self, answers=(), supported=True, rejects=(), check_error=None):
        self.answers, self.supported, self.rejects, self.check_error = list(answers), supported, rejects, check_error
        self.prompts: list[str] = []
        self.note_checks: list[str] = []

    def chat(self, system, user, max_tokens, schema=None, timeout=240):
        self.prompts.append(system + "\n" + user)
        return self.answers.pop(0) if self.answers else NOT_YET

    def chat_json(self, system, user, max_tokens, schema, timeout=240):
        self.prompts.append(system + "\n" + user)
        if "notes about a book" in system:
            self.note_checks.append(user)
            if self.check_error:
                raise self.check_error
            return {"supported": [int(n) for n, line in re.findall(r"^Note (\d+) (.*)$", user, re.M)
                                  if not any(r in line for r in self.rejects)]}
        return {"supported": self.supported}

    def busy(self):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="marginalia-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.memory = Memory(os.path.join(self.dir, "m.db"))
        self.addCleanup(self.memory.close)
        self.memory.open_book(BOOK, "Test", 200, 50)
        self.memory.add_passages(BOOK, [Passage(0, 0, 50, "One", EARLY), Passage(1, 50, 100, "Two", LATE)])


class Lock(Base):
    """The whole point: nothing past the lock can be selected, by any route."""

    def test_search_stops_at_the_lock(self):
        self.assertEqual([p.idx for p in self.memory.search(BOOK, "Tomas mill", 50, 10)], [0])
        self.assertEqual({p.idx for p in self.memory.search(BOOK, "Tomas mill", 100, 10)}, {0, 1})
        self.assertEqual(self.memory.search(BOOK, "burned winter", 50, 10), [])

    def test_passages_stop_at_the_lock(self):
        self.assertEqual([p.idx for p in self.memory.passages_between(BOOK, 0, 9, 50)], [0])

    def test_characters_facts_and_talk_stop_at_the_lock(self):
        anna = self.memory.add_character(BOOK, "Anna", [], 50)
        later = self.memory.add_character(BOOK, "Ilse", [], 100)
        self.memory.add_fact(anna, 50, 0, "Anna has a key", "brass key")
        self.memory.add_fact(later, 100, 1, "Ilse left", "the mill")
        self.memory.add_fact(anna, 100, 1, "Anna was betrayed", "betrayed Anna")
        self.memory.add_talk(anna, 50, "early?", "yes", "One")
        self.memory.add_talk(anna, 100, "late?", "yes", "Two")
        self.assertEqual([c.name for c in self.memory.characters(BOOK, 50)], ["Anna"])
        self.assertEqual(self.memory.facts(anna, 50, 9), ["Anna has a key"])
        self.assertEqual([r["question"] for r in self.memory.talk(anna, 50, 9)], ["early?"])
        self.assertEqual({c.id for c in self.memory.characters(BOOK, 100)}, {anna, later})

    def test_the_is_not_part_of_a_name(self):
        self.assertEqual(self.memory.add_character(BOOK, "The Captain", [], 50), self.memory.add_character(BOOK, "Captain", [], 50))

    def test_an_alias_learned_later_stays_hidden(self):
        anna = self.memory.add_character(BOOK, "Anna", [], 50)
        self.assertEqual(self.memory.add_character(BOOK, "The Heir", ["Anna"], 100), anna)
        self.assertEqual(self.memory.known_names(anna, 50), ["anna"])
        self.assertIn("heir", self.memory.known_names(anna, 100))

    def test_later_text_never_reaches_the_prompt(self):
        anna = self.memory.add_character(BOOK, "Anna", [], 50)
        self.memory.add_fact(anna, 50, 0, "Anna has a key", "brass key")
        self.memory.add_fact(anna, 100, 1, "Anna was betrayed", "betrayed Anna")
        self.memory.add_talk(anna, 100, "Who burned the mill?", "Tomas did", "Two")
        who = self.memory.characters(BOOK, 50)[0]
        system, user, _ = build_prompt(self.memory, BOOK, who, "Who burned the mill? Tomas winter", 50, "")
        whole = system + user
        for secret in ("betrayed", "burned the mill to", "Tomas did"):
            self.assertNotIn(secret, whole)
        self.assertIn("brass key", whole)


class Budget(Base):
    def test_a_prompt_stays_within_its_budget(self):
        huge = "word " * PASSAGE_WORDS
        self.memory.add_passages(BOOK, [Passage(i, 50, 50, "x", f"Anna mill {huge}") for i in range(2, 40)])
        anna = self.memory.add_character(BOOK, "Anna", [], 50)
        self.memory.add_fact(anna, 50, 0, "Anna has a key", "brass key")
        for i in range(30):
            self.memory.add_fact(anna, 50, 0, f"Anna fact number {i} " + "detail " * 10, "brass key")
            self.memory.add_talk(anna, 50, f"question {i} " * 20, f"answer {i} " * 20, "One")
        who = self.memory.characters(BOOK, 50)[0]
        system, user, numbered = build_prompt(self.memory, BOOK, who, "Tell me about Anna and the mill", 50, "recent " * 120)
        self.assertLessEqual(tokens(system) + tokens(user), ask_module.INPUT_TOKENS + 30)
        self.assertTrue(numbered)


class Answers(Base):
    def setUp(self):
        super().setUp()
        anna = self.memory.add_character(BOOK, "Anna", [], 50)
        self.memory.add_fact(anna, 50, 0, "Anna has a key", "brass key")
        self.who = next(c for c in self.memory.characters(BOOK, 50) if c.id == anna)

    def reply(self, model):
        return ask(self.memory, model, BOOK, self.who, "Where is the key?", 50, "")

    def test_a_checked_answer_comes_back_clean(self):
        r = self.reply(FakeModel(["Under the mill wheel [1]."]))
        self.assertEqual((r.text, r.sources, r.kept), ("Under the mill wheel.", "One", True))

    def test_a_failed_check_is_withheld(self):
        r = self.reply(FakeModel(["Tomas burned the mill [1]."], supported=False))
        self.assertEqual((r.text, r.kept), (UNSURE, False))
        self.assertNotIn("burned", r.text)

    def test_no_citation_is_withheld(self):
        self.assertEqual(self.reply(FakeModel(["Everyone knows Tomas did it."])).kept, False)

    def test_a_citation_it_was_never_given_is_withheld(self):
        self.assertEqual(self.reply(FakeModel(["It was Tomas [9]."])).kept, False)

    def test_not_yet_needs_no_check(self):
        model = FakeModel([NOT_YET])
        self.assertEqual(self.reply(model).text, NOT_YET)
        self.assertEqual(len(model.prompts), 1)


class Notes(Base):
    def notes(self, reply):
        batch = self.memory.passages_between(BOOK, 0, 1, 50)
        return read_notes(self.memory, FakeModel([reply]), BOOK, batch)

    def test_a_fact_with_a_real_quote_is_kept(self):
        self.assertEqual(self.notes('Anna | Anna has a key | "found the brass key"'), 1)
        self.assertEqual([c.name for c in self.memory.characters(BOOK, 50)], ["Anna"])

    def test_a_quote_not_in_the_text_is_dropped(self):
        self.assertEqual(self.notes("Anna | Anna is betrayed | Tomas betrayed Anna"), 0)
        self.assertEqual(self.memory.characters(BOOK, 100), [])

    def test_a_name_not_in_the_text_is_dropped(self):
        self.assertEqual(self.notes("Ilse | she holds a key | the brass key"), 0)
        self.assertEqual(self.memory.characters(BOOK, 100), [])

    def test_another_name_counts_only_if_it_is_in_the_text(self):
        self.notes("Anna | Anna has a key | the brass key\nALSO | Anna | The Heir\nALSO | Tomas | Anna")
        self.assertEqual(self.memory.known_names(self.memory.characters(BOOK, 50)[0].id, 100), ["anna"])

    def test_a_reply_cut_off_keeps_what_came_first(self):
        self.assertEqual(self.notes("Anna | Anna has a key | the brass key\nTomas | Tomas had a ke"), 1)

    def test_filling_in_the_form_with_the_form_is_dropped(self):
        self.assertEqual(self.notes("NAME | a man | the brass key\nAnna | named in this passage | the brass key"), 0)

    def test_a_name_in_capitals_is_tidied(self):
        self.assertEqual(self.notes("ANNA | she has a key | the brass key"), 1)
        self.assertEqual(self.memory.characters(BOOK, 50)[0].name, "Anna")

    def test_the_author_is_not_a_character(self):
        batch = self.memory.passages_between(BOOK, 0, 1, 50)
        kept = read_notes(self.memory, FakeModel(["Anna | wrote this | the brass key\nTomas | was kept in the dark | hid it from Tomas"]),
                          BOOK, batch, frozenset({"anna"}))
        self.assertEqual(kept, 1)
        self.assertEqual([c.name for c in self.memory.characters(BOOK, 50)], ["Tomas"])

    def test_nonsense_from_the_model_is_harmless(self):
        self.assertEqual(self.notes("Sure! Here are the notes:\n\n| | |\na|b\n{json}"), 0)


class Splitting(unittest.TestCase):
    def test_every_word_lands_in_one_passage_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            body = "".join(f"<p>{'word ' * n}end{i}</p>" for i, n in enumerate([5, 50, 900, 3, 400, 1, 299, 300, 301]))
            path = make_book(os.path.join(d, "b.epub"), [("a.xhtml", "<h1>Start</h1>" + body), ("b.xhtml", "<p>" + "last " * 40 + "</p>")])
            book = ebook.load(path)
        passages = split_passages(book)
        self.assertEqual(passages[0].word_start, 0)
        self.assertEqual(passages[-1].word_end, book.words)
        at = 0
        for p in passages:
            self.assertEqual(p.word_start, at, "no gap or overlap")
            self.assertEqual(p.word_end - p.word_start, len(p.text.split()))
            self.assertLessEqual(p.word_end - p.word_start, PASSAGE_WORDS)
            at = p.word_end
        self.assertEqual(sum(len(p.text.split()) for p in passages), book.words)


class Text(unittest.TestCase):
    def test_quotes_compare_with_any_punctuation_style(self):
        self.assertEqual(normal("It’s  “Fine”\n"), normal("it's \"fine\""))


try:
    import anthropic
except ImportError:
    anthropic = None


class FakeClaude(BaseHTTPRequestHandler):
    """Anthropic's Messages endpoint, as far as ClaudeModel can tell. Nothing here reaches the real one."""
    seen: list[tuple[dict, dict]] = []
    status = 200
    error = ""
    stop = "end_turn"
    text = "Hello [1]."

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeClaude.seen.append((dict(self.headers), body))
        if self.status != 200:
            out = {"type": "error", "error": {"type": "invalid_request_error", "message": self.error}}
        else:
            out = {"id": "m", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": self.stop,
                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
                   "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": self.text}]}
        data = json.dumps(out).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(anthropic, "the anthropic package isn't installed")
class Claude(unittest.TestCase):
    def setUp(self):
        FakeClaude.seen, FakeClaude.status, FakeClaude.stop, FakeClaude.text = [], 200, "end_turn", "Hello [1]."
        self.server = HTTPServer(("127.0.0.1", 0), FakeClaude)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def model(self, name="claude-opus-5-5"):
        return ClaudeModel("k", name, self.url)

    def test_the_request_suits_a_model_that_always_thinks(self):
        self.assertEqual(self.model().chat("sys", "hi", 60), "Hello [1].")
        headers, body = FakeClaude.seen[0]
        self.assertEqual((body["system"], body["messages"]), ("sys", [{"role": "user", "content": "hi"}]))
        self.assertEqual(body["max_tokens"], CLAUDE_MIN_TOKENS)     # 60 would leave no room after thinking
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", body)                       # Claude turns these down
        self.assertNotIn("thinking", body)
        self.assertEqual(body["fallbacks"], "default")
        self.assertIn("server-side-fallback", headers.get("anthropic-beta", headers.get("Anthropic-Beta", "")))

    def test_haiku_has_no_fallback(self):
        self.model("claude-haiku-5-5").chat("sys", "hi", 60)
        self.assertNotIn("fallbacks", FakeClaude.seen[0][1])

    def test_json_replies_use_the_schema(self):
        FakeClaude.text = '{"supported": true}'
        schema = ask_module.CHECK_SCHEMA
        self.assertEqual(self.model().chat_json("sys", "hi", 60, schema), {"supported": True})
        self.assertEqual(FakeClaude.seen[0][1]["output_config"]["format"], {"type": "json_schema", "schema": schema})

    def test_a_refusal_reads_as_nothing_to_say(self):
        FakeClaude.stop = "refusal"
        self.assertEqual(self.model().chat("sys", "hi", 60), "")

    def test_a_refused_check_withholds_the_answer(self):
        FakeClaude.stop = "refusal"
        with self.assertRaises(ModelError):
            self.model().chat_json("sys", "hi", 60, ask_module.CHECK_SCHEMA)

    def test_errors_say_what_went_wrong(self):
        for status, error, says in ((401, "bad", "refused the key"), (403, "no", "can't use"),
                                    (400, "Your credit balance is too low", "no API credit"), (400, "nope", "Claude said 400")):
            FakeClaude.status, FakeClaude.error = status, error
            with self.assertRaises(ModelError) as caught:
                self.model().chat("sys", "hi", 60)
            self.assertIn(says, str(caught.exception))

    def test_unreachable(self):
        with self.assertRaises(ModelError) as caught:
            ClaudeModel("k", url="http://127.0.0.1:9").chat("sys", "hi", 60, timeout=2)
        self.assertIn("isn't answering", str(caught.exception))


class NoteCheck(Base):
    """A real quote beside a fact from elsewhere in the book: a model that knows the book could write that."""
    KNOWN = "Anna | Anna secretly murders the king in the final chapter | hid it from Tomas"
    FINE = "Anna | Anna has a key | found the brass key"

    def read(self, reply, **options):
        self.model = FakeModel([reply], **options)
        return read_notes(self.memory, self.model, BOOK, self.memory.passages_between(BOOK, 0, 1, 50))

    def test_a_fact_its_passage_does_not_support_is_dropped(self):
        self.assertEqual(self.read(f"{self.KNOWN}\n{self.FINE}", rejects=("murders",)), 1)
        anna = self.memory.characters(BOOK, 50)[0]
        self.assertEqual(self.memory.facts(anna.id, 50, 10), ["Anna has a key"])

    def test_someone_with_only_unsupported_facts_is_not_met(self):
        self.assertEqual(self.read(self.KNOWN, rejects=("murders",)), 0)
        self.assertEqual(self.memory.characters(BOOK, 50), [])

    def test_a_declined_check_keeps_nothing_and_does_not_ask_again(self):
        self.assertEqual(self.read(self.FINE, check_error=BadReply("declined")), 0)
        self.assertEqual(self.memory.characters(BOOK, 50), [])

    def test_a_model_out_of_reach_means_trying_the_batch_again(self):
        with self.assertRaises(ModelError):
            self.read(self.FINE, check_error=ModelError("The model isn't answering"))
        self.assertEqual(self.memory.characters(BOOK, 50), [])

    def test_nothing_to_check_costs_nothing(self):
        self.assertEqual(self.read("Ilse | she holds a key | the brass key"), 0)
        self.assertEqual(self.model.note_checks, [])

    def test_a_quote_of_two_words_is_too_little(self):
        self.assertEqual(self.read("Anna | Anna murders the king | brass key"), 0)
        self.assertEqual(self.model.note_checks, [])

    def test_only_numbers_of_real_notes_count(self):
        model = FakeModel()
        model.chat_json = lambda *a, **k: {"supported": [True, 0, 2, 99, "1"]}
        self.assertEqual(read_notes_with(self.memory, model, self.FINE), 0)

    def test_each_note_is_judged_on_its_own_passage(self):
        self.memory.set_lock(BOOK, 100)
        model = FakeModel(["Anna | Anna has a key | found the brass key"])
        read_notes(self.memory, model, BOOK, self.memory.passages_between(BOOK, 0, 2, 100))
        self.assertIn(EARLY, model.note_checks[0])
        self.assertNotIn(LATE, model.note_checks[0])


def read_notes_with(memory, model, reply):
    model.chat = lambda *a, **k: reply
    return read_notes(memory, model, BOOK, memory.passages_between(BOOK, 0, 1, 50))


class Waiting(unittest.TestCase):
    def test_it_waits_longer_after_each_failure_up_to_a_limit(self):
        self.assertEqual([error_wait(n) for n in (1, 2, 3)], [30.0, 60.0, 120.0])
        self.assertEqual(error_wait(50), MAX_ERROR_WAIT)


class Choosing(unittest.TestCase):
    def test_off_unless_asked_for_even_with_a_key(self):
        self.assertIsNone(model_from({"model_key": "k"}))
        self.assertIsNone(model_from({"provider": "off", "model_key": "k"}))

    def test_no_key_no_model(self):
        self.assertIsNone(model_from({"provider": "claude"}))

    def test_each_provider(self):
        claude = model_from({"provider": "claude", "model_key": "k"})
        self.assertIsInstance(claude, ClaudeModel)
        self.assertEqual(claude.name, "claude-opus-5-5")
        self.assertFalse(claude.busy())
        self.assertIsInstance(model_from({"provider": "llamacpp", "model_key": "k"}), Model)

    def test_the_key_can_live_in_another_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"claude": {"api_key": "from-file"}}, f)
            model = model_from({"provider": "claude", "model_key_file": path, "model_key_path": "claude.api_key"})
            self.assertEqual(model.key, "from-file")


if __name__ == "__main__":
    unittest.main()
