"""Shared helpers for chat tests: a scripted fake model and an SSE reader.

`FakeChatModel` stands in for Claude (like FakeEmailSender for email): a test
scripts the replies and then inspects exactly what the app sent. No network
calls, no API cost.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from httpx import AsyncClient

from app.services.llm import ModelReply, StreamItem, TextDelta
from app.services.pricing import TokenUsage

# What a Haiku 5.5 reply looks like: an (empty-text) thinking block with a
# signature, then the answer.
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def reply(
    text: str = "Hello!",
    *,
    chunks: list[str] | None = None,
    stop_reason: str = "end_turn",
    content: list[dict[str, Any]] | None = None,
    usage: TokenUsage | None = None,
) -> list[StreamItem]:
    """A scripted reply: its text deltas, then the final ModelReply."""
    deltas = [TextDelta(c) for c in (chunks if chunks is not None else [text])]
    final = ModelReply(
        content=content if content is not None else [THINKING, {"type": "text", "text": text}],
        stop_reason=stop_reason,
        usage=usage or TokenUsage(input_tokens=1_000, output_tokens=200),
        model="claude-haiku-5-5",
    )
    return [*deltas, final]


@dataclass
class FakeChatModel:
    replies: list[list[StreamItem] | Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamItem]:
        # Deep copy via JSON: later turns must not mutate what we recorded.
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        script = self.replies.pop(0)
        if isinstance(script, Exception):
            raise script
        for item in script:
            yield item


async def new_conversation(client: AsyncClient) -> str:
    response = await client.post("/chat/conversations")
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def send(
    client: AsyncClient, conversation_id: str, text: str
) -> list[tuple[str, dict[str, Any]]]:
    """Send a message and parse the SSE response into (event, data) pairs."""
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": text}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for frame in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def tool_use(name: str, input: dict[str, Any], id: str = "toolu_1") -> dict[str, Any]:
    """A tool_use content block, as the API returns it."""
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def wants_tools(*blocks: dict[str, Any], usage: TokenUsage | None = None) -> list[StreamItem]:
    """A scripted reply that asks for tools (stop_reason tool_use)."""
    return reply(chunks=[], content=[THINKING, *blocks], stop_reason="tool_use", usage=usage)
