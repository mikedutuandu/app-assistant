"""The graph's four nodes, one async function each: `(state, turn) -> state update` (`build.py` wires them).

- `read_files` starts the turn: reads the message's files and links.
- `load_context` gets the user's voice, topics and recent posts from the app (cached for 10 minutes).
- `call_model` makes one call of the chat model; when it calls tools, the graph goes to
- `run_tools`, which runs them and goes back to `call_model`, until the model answers in text.

The last two share the turn's work through `agent.AgentTurn`. Every model call first checks the message's cost cap:
reaching it ends the turn with the cap's line, keeping what the turn already did.
"""

import re
import time

from langchain_core.messages import HumanMessage

from assistant.checks import nfc
from assistant.files.intake import UploadError
from assistant.graph.agent import AgentTurn
from assistant.graph.model_input import message_text
from assistant.graph.state import ChatState
from assistant.graph.turn import CAP_TEXT, Capped, Turn
from assistant.llm import LlmError

CONTEXT_TTL = 600  # seconds the app's context is reused within a thread
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


def _latest_user_text(state: ChatState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return nfc(message_text(m))
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
    t.deps.agent = None  # the agent's work starts afresh in this turn's first call_model
    update: dict = {"file_ids": [], "capped": False, "failed": False, "tools_pending": False}
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
    """The app's context (voice, topics, recent posts), reused for 10 minutes within the thread."""
    if state.get("context") is not None and 0 <= _now() - state.get("context_at", 0.0) < CONTEXT_TTL:
        return {}
    ctx = await t.deps.tools_factory().context()
    kept = [k.model_copy(update={"text": nfc(k.text).strip()}) for k in ctx.kept_facts[:MAX_KEPT]]
    ctx = ctx.model_copy(
        update={"recent_posts": [nfc(p) for p in ctx.recent_posts], "kept_facts": [k for k in kept if k.text]}
    )
    name = nfc(ctx.name or "").strip() or None
    return {"context": ctx, "context_at": _now(), "user_name": name}


async def call_model(state: ChatState, turn: Turn) -> dict:
    """One call of the chat model, its text streamed to the user. When it calls tools, `run_tools` runs them next;
    otherwise the turn's reply is done."""
    agent = turn.deps.agent
    if agent is None:  # the turn's first call
        await turn.emit("status", {"step": "thinking"})
        agent = turn.deps.agent = AgentTurn.begin(state, turn)
    try:
        called_tools = await agent.call_model(turn)
    except Capped:
        await agent.reply.show_cap()
        return agent.update(done=True, capped=True)
    except LlmError as e:
        return _failed(agent, e)
    return agent.update(done=not called_tools)


async def run_tools(state: ChatState, turn: Turn) -> dict:
    """Runs the tools the chat model just called; the graph goes back to `call_model` with their results."""
    agent: AgentTurn = turn.deps.agent
    try:
        await agent.run_tools(turn)
    except Capped:
        await agent.reply.show_cap()
        return agent.update(done=True, capped=True)
    except LlmError as e:
        return _failed(agent, e)
    return agent.update(done=False)


def _failed(agent: AgentTurn, error: LlmError) -> dict:
    """A model call failed. Before any tool ran, the turn fails as any failed call does (`bind` carries its cost).
    After tools ran (drafts shown, facts saved in the app), their work is kept in the thread, so a later "shorter"
    finds the draft and the facts aren't saved again, and the turn ends as failed."""
    if not agent.results:
        raise error
    agent.reply.drop_round()  # what the failed call had started to say is not kept
    return agent.update(done=True, failed=True)
