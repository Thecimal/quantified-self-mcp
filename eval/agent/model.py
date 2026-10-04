"""Model clients for the agent eval.

A model client takes the system prompt, the running message list and the
live tool schemas, and returns one Anthropic Messages API response dict
({"content": [...blocks], "stop_reason": ...}). AnthropicHTTP talks to the
real API over urllib (no SDK dependency); ScriptedModel replays prepared
responses so the harness itself can be tested without a model or a key.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

API_URL = "https://api.anthropic.com/v1/messages"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})


class ModelError(RuntimeError):
    """The model could not be reached, or a scripted model ran out of responses."""


class ModelClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]: ...


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(name: str, arguments: dict[str, Any], block_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def reply(*blocks: dict[str, Any]) -> dict[str, Any]:
    """One model response made of the given content blocks."""
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return {"content": list(blocks), "stop_reason": stop}


class AnthropicHTTP:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 1024,
        retries: int = 3,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._retries = retries
        self._urlopen = urlopen
        self._sleep = sleep

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": self.name,
                "max_tokens": self._max_tokens,
                "system": system,
                "tools": tools,
                "tool_choice": {"type": "auto"},
                "messages": messages,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        for attempt in range(1, self._retries + 1):
            try:
                with self._urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in RETRYABLE_STATUS and attempt < self._retries:
                    self._sleep(2 ** (attempt - 1))
                    continue
                raise ModelError(f"Anthropic API error {exc.code}: {detail}") from exc
        raise ModelError("no attempts were made")


class ScriptedModel:
    """Replays prepared responses in order; an entry may be a callable taking the message list."""

    def __init__(self, responses: list[Any], name: str = "scripted") -> None:
        self.name = name
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        if not self._responses:
            raise ModelError("scripted model ran out of responses")
        response = self._responses.pop(0)
        return response(messages) if callable(response) else response
