"""Talks to Claude through Anthropic's API, or to a local llama.cpp server over its OpenAI-style API. No streaming: an
answer is shown only once it has been checked, so there is nothing to gain from seeing it arrive. Claude needs the
`anthropic` package, imported only when it's first called; llama.cpp needs only the standard library."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "low"           # short notes and answers, and a yes or no
# Claude's thinking counts towards its token limit and can't be turned off, so the small limits that keep the local
# model quick would leave it no room to answer. How long a reply is comes from the instructions instead.
CLAUDE_MIN_TOKENS = 16000
# If a safety classifier declines a request (a novel's violence, say), Anthropic re-runs it on the model it
# recommends. Haiku has no such fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelError(Exception):
    pass


class BadReply(ModelError):
    """The model answered, but not with what was asked for, as when it declines. Asking again won't help."""


def parse_object(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BadReply("The model's reply wasn't valid JSON") from e
    if not isinstance(data, dict):
        raise BadReply("The model's reply wasn't an object")
    return data


class Model:
    def __init__(self, url: str, key: str, name: str):
        self.url, self.key, self.name = url.rstrip("/"), key, name

    def _request(self, path: str, body: dict | None, timeout: float) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as reply:
                return json.load(reply)
        except urllib.error.HTTPError as e:
            raise ModelError("The model refused the key" if e.code in (401, 403) else f"The model said {e.code}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
            raise ModelError("The model isn't answering") from e

    def chat(self, system: str, user: str, max_tokens: int, schema: dict | None = None, timeout: float = 240) -> str:
        body = {"model": self.name, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
        reply = self._request("/chat/completions", body, timeout)
        try:
            return (reply["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise ModelError("The model's reply made no sense") from e

    def chat_json(self, system: str, user: str, max_tokens: int, schema: dict, timeout: float = 240) -> dict:
        return parse_object(self.chat(system, user, max_tokens, schema, timeout))

    def busy(self) -> bool:
        """Is someone else using the only slot? Preparing a book waits for them. Unknown counts as free."""
        root = self.url[:-3] if self.url.endswith("/v1") else self.url
        try:
            req = urllib.request.Request(root + "/slots", headers={"Authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=3) as reply:
                return any(s.get("is_processing") for s in json.load(reply))
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError, AttributeError, TypeError):
            return False


class ClaudeModel:
    """The same calls as Model, through Anthropic's API. `url` is only for trying it against a stand-in."""

    def __init__(self, key: str, name: str = CLAUDE_MODEL, url: str | None = None):
        self.key, self.name, self.url = key, name, url
        self.client = None

    def _connect(self):
        try:
            import anthropic
        except ImportError as e:
            raise ModelError("The anthropic package isn't installed. See docs/bookread.md.") from e
        if self.client is None:
            self.client = anthropic.Anthropic(api_key=self.key, base_url=self.url, max_retries=1)
        return anthropic

    def chat(self, system: str, user: str, max_tokens: int, schema: dict | None = None, timeout: float = 240) -> str:
        """The reply's text: empty if Claude declined, which reads as having nothing to say, and doesn't ask again."""
        anthropic = self._connect()
        output = {"effort": CLAUDE_EFFORT}
        if schema:
            output["format"] = {"type": "json_schema", "schema": schema}
        request = dict(model=self.name, max_tokens=max(max_tokens, CLAUDE_MIN_TOKENS), system=system, output_config=output,
                       messages=[{"role": "user", "content": user}])
        client = self.client.with_options(timeout=timeout)
        try:
            if self.name.startswith("claude-haiku"):
                message = client.messages.create(**request)
            else:
                message = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
        except anthropic.AuthenticationError as e:
            raise ModelError("Anthropic refused the key") from e
        except anthropic.PermissionDeniedError as e:
            raise ModelError(f"This key can't use {self.name}") from e
        except anthropic.RateLimitError as e:
            raise ModelError("Claude is busy, or the account's limit is reached") from e
        except anthropic.APIStatusError as e:
            body = e.body if isinstance(e.body, dict) else {}
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            if "credit" in str(error.get("message") or e.message).lower():
                raise ModelError("The Anthropic account has no API credit") from e
            raise ModelError(f"Claude said {e.status_code}") from e
        except anthropic.APIError as e:     # can't connect, or timed out
            raise ModelError("Claude isn't answering") from e
        if message.stop_reason == "refusal":
            return ""
        return "".join(block.text for block in message.content if block.type == "text").strip()

    def chat_json(self, system: str, user: str, max_tokens: int, schema: dict, timeout: float = 240) -> dict:
        return parse_object(self.chat(system, user, max_tokens, schema, timeout))

    def busy(self) -> bool:
        return False        # there's no single slot to wait for
