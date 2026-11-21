"""The reply's text as the user sees it, streamed round by round.

A turn can call the chat model several times (`call_model`, then `run_tools`, then `call_model` again). The first
round's text streams as it comes. A later round's is held until the round ends: after its tools the model sometimes
says again what it said before them, and the user must never get the same words twice, so held text is shown only
for what it adds. Every event of the turn goes through `emit`, so text knows whether it continues the bubble on
screen (after a draft, a status or a saved line, the next text starts a new one).
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from assistant.checks import nfc
from assistant.graph.turn import CAP_TEXT

BUBBLE_ENDS = (
    "status",
    "draft",
    "saved",
    "confirm",
)  # events the widget shows outside a text bubble: the next text starts anew


def said_key(text: str) -> str:
    """How a reply's text is compared with an earlier round's: case and spacing aside."""
    return " ".join(nfc(text).casefold().split())


def is_repeat(new: str, old: str) -> bool:
    """The same words, or one a whole-word run inside the other that makes up most of it (>= 0.6 of its words)."""
    a, b = re.findall(r"\w+", new), re.findall(r"\w+", old)
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not short or len(short) < 0.6 * len(long):
        return False
    return any(long[i : i + len(short)] == short for i in range(len(long) - len(short) + 1))


def text_after(text: str, key: str) -> str:
    """What `text` says after its start, which reads as `key` (see `said_key`)."""
    for i in range(len(text) + 1):
        if said_key(text[:i]) == key:
            return text[i:]
    return ""


@dataclass
class ReplyText:
    send: Callable[[str, dict], Awaitable[None]]  # the turn's event stream (Turn.emit)
    parts: list[str] = field(default_factory=list)  # what the model said, round by round (shown text only)
    said: str = ""  # this round's text so far, as the model said it
    shown: str = ""  # this round's text as the user saw it (a repeat of an earlier round is never shown)
    held: str = ""  # a later round's text, kept back until the round ends
    in_bubble: bool = False  # the last thing shown was this turn's text: more text continues the same bubble

    async def emit(self, kind: str, payload: dict) -> None:
        """Sends any event of the turn, keeping track of whether a text bubble is open."""
        if kind == "text":
            self.in_bubble = True
        elif kind in BUBBLE_ENDS:
            self.in_bubble = False
        await self.send(kind, payload)

    async def forward(self, delta: str) -> None:
        """Takes the model's streamed text: shown at once in the first round, held in a later one."""
        if not delta:
            return
        self.said += delta
        if self.parts:
            self.held += delta
            return
        await self._show(delta)

    async def _show(self, text: str) -> None:
        if not self.shown and self.parts and self.in_bubble:  # an earlier round's text is still the last thing shown
            await self.emit("text", {"delta": "\n\n"})  # a new paragraph; after a draft, a new bubble needs none
        self.shown += text
        await self.emit("text", {"delta": text})

    async def release(self) -> None:
        """Shows a later round's held text, unless it repeats an earlier round's (the same words, case and spacing
        aside, or one inside the other); text that only adds to an earlier round shows what it adds."""
        held, self.held = self.held, ""
        new = said_key(held)
        if not new:
            return
        for part in self.parts:
            old = said_key(part)
            if new.startswith(old) and new != old:
                held = text_after(held, old)
                new = said_key(held)
            if not new or new == old or is_repeat(new, old):
                return
        await self._show(held.lstrip())

    def end_round(self) -> None:
        """Closes the round: what the user saw of it joins `parts`."""
        if self.shown.strip():
            self.parts.append(self.shown.strip())
        self.said = self.shown = self.held = ""

    def drop_round(self) -> None:
        """Forgets a round that failed part way: what it had started to say is not kept."""
        self.said = self.shown = self.held = ""

    async def show_cap(self) -> None:
        """Ends the reply with the cost cap's line, in a paragraph of its own."""
        self.end_round()
        if self.parts and self.in_bubble:
            await self.emit("text", {"delta": "\n\n"})
        await self.emit("text", {"delta": CAP_TEXT})

    @property
    def reply(self) -> str:
        """Everything the user saw, as the thread keeps it."""
        return "\n\n".join(self.parts)
