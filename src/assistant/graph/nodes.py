"""The graph's first two nodes, one async function each: `(state, turn) -> state update`. The third, `converse`,
is the conversation agent (`agent.py`).

The only app tool these reach is `context()`. Every model call (reading a file) first checks the message's cost cap.
"""

import re
import time

from langchain_core.messages import BaseMessage, HumanMessage

from assistant.checks import nfc
from assistant.files.intake import UploadError
from assistant.graph.state import ChatState
from assistant.graph.turn import CAP_TEXT, Capped, Turn

CONTEXT_TTL = 300  # seconds the app's context is reused within a thread
MAX_LINKS = 2  # links read from one message
MAX_KEPT = 10  # kept facts taken from the app's context

READ_NOTES = {
    "voice": "Read your voice note",
    "image": "Read your screenshot",
    "pdf": "Read your PDF",
    "link": "Read the link",
}

_LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_now = time.time


def _content(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(p.get("text", "") for p in message.content if isinstance(p, dict))


def _latest_user_text(state: ChatState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return nfc(_content(m))
    return ""


def _links(text: str) -> list[str]:
    found: list[str] = []
    for m in _LINK.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}")
        if url not in found:
            found.append(url)
    return found[:MAX_LINKS]


# ---- nodes ----


async def read_files(state: ChatState, t: Turn) -> dict:
    """Starts the turn: resets its work, reads this message's files and up to 2 links in its text."""
    t.cost = 0.0
    update: dict = {"file_ids": [], "capped": False, "failed": False}
    readings = list(state.get("readings", []))
    seen = {r.file_id for r in readings}
    jobs = [(f, False) for f in state.get("file_ids", [])] + [(u, True) for u in _links(_latest_user_text(state))]
    try:
        for ref, is_link in jobs:
            t.gate()
            if is_link:
                await t.emit("status", {"step": "reading"})
            try:  # the reader reports each model call's cost to the turn as it ends, and checks the cap before each
                got = [await t.deps.read_link(ref)] if is_link else await t.deps.read_files([ref], t)
            except UploadError as e:
                await t.emit("text", {"delta": e.message + "\n"})
                continue
            for r in got:
                r = r.model_copy(update={"text": nfc(r.text), "unsure": [nfc(u) for u in r.unsure]})
                if r.kind == "voice":
                    await t.emit("transcript", {"file_id": r.file_id, "text": r.text})
                await t.note(READ_NOTES.get(r.kind, "Read your file"))
                if r.file_id not in seen:
                    seen.add(r.file_id)
                    readings.append(r)
    except Capped:
        await t.emit("text", {"delta": CAP_TEXT})
        update["capped"] = True
    return {**update, "readings": readings}


async def load_context(state: ChatState, t: Turn) -> dict:
    """The app's context (voice, topics, recent posts), reused for 5 minutes within the thread."""
    if state.get("context") is not None and 0 <= _now() - state.get("context_at", 0.0) < CONTEXT_TTL:
        return {}
    ctx = await t.deps.tools_factory().context()
    kept = [k.model_copy(update={"text": nfc(k.text).strip()}) for k in ctx.kept_facts[:MAX_KEPT]]
    ctx = ctx.model_copy(
        update={"recent_posts": [nfc(p) for p in ctx.recent_posts], "kept_facts": [k for k in kept if k.text]}
    )
    name = nfc(ctx.name or "").strip() or None
    return {"context": ctx, "context_at": _now(), "user_name": name}
