"""The graph's nodes, one async function each: `(state, turn) -> state update`.

The graph has no free tool calling: the only app tool it reaches is `context()`. Everything a user, a file, a link
or the app supplied goes to a model inside a `<data>` block. Every model call first checks the message's cost cap.
"""

import re
import time
from collections import Counter

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict

from assistant.checks import Fact, nfc, numbers_in, unsupported_numbers
from assistant.db import new_id
from assistant.files.intake import UploadError
from assistant.graph import prompts
from assistant.graph.prompts import data
from assistant.graph.state import ChatState, Piece
from assistant.graph.turn import CAP_TEXT, Capped, Turn
from assistant.packs import AppPack, WriteSpec
from assistant.tools import KeptFact
from assistant.writer import (
    CLAIM,
    CLAIM_TOKENS,
    ClaimOut,
    claim_request,
    fact_blocks,
    image_allowed,
    language_tag,
    post_problems,
)

CONTEXT_TTL = 300  # seconds the app's context is reused within a thread
MAX_ROUNDS = 2  # ask-back rounds before writing with what there is
MAX_LINKS = 2  # links read from one message
MAX_MESSAGES = 20  # conversation messages shown to a model
MAX_KEPT = 10  # kept facts taken from the app's context

READ_NOTES = {
    "voice": "Read your voice note",
    "image": "Read your screenshot",
    "pdf": "Read your PDF",
    "link": "Read the link",
}

_LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
INTRO = "Here's what I wrote from what you told me:"
KEPT_OFFER = "Last time you kept these — want to write from one?"
KEPT_NOTE = "Offered the facts you kept"
NOTHING_WRITTEN = "I couldn't write anything from that yet. Tell me a bit more?"
_now = time.time


# ---- structured outputs ----


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterviewOut(_Strict):
    enough: bool
    just_write: bool
    question: str | None


class FactOut(_Strict):
    text: str
    source: str
    file_id: str | None


class ExtractOut(_Strict):
    facts: list[FactOut]


class PieceOut(_Strict):
    spec_kind: str
    text: str
    fact_ids: list[str]
    image_file_id: str | None


class WriterOut(_Strict):
    pieces: list[PieceOut]


# ---- helpers ----


def _key(text: str) -> str:
    return " ".join(nfc(text).casefold().split())


def _kept_key(text: str) -> str:
    """How an extracted fact is matched to a kept one: case, spacing and end punctuation aside."""
    return _key(text).strip(" .!?;:,")


def _content(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(p.get("text", "") for p in message.content if isinstance(p, dict))


def _latest_user_text(state: ChatState) -> str:
    for m in reversed(state.get("messages", [])):
        if isinstance(m, HumanMessage):
            return nfc(_content(m))
    return ""


def _kept(state: ChatState) -> list[KeptFact]:
    ctx = state.get("context")
    return list(ctx.kept_facts) if ctx else []


def _source_texts(state: ChatState) -> list[str]:
    """What the user said and showed: this thread's messages, the readings' text (not `unsure`), and the facts they
    kept in an earlier chat (their own words, told then)."""
    said = [nfc(_content(m)) for m in state.get("messages", []) if isinstance(m, HumanMessage)]
    return said + [r.text for r in state.get("readings", [])] + [k.text for k in _kept(state)]


def _links(text: str) -> list[str]:
    found: list[str] = []
    for m in _LINK.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}")
        if url not in found:
            found.append(url)
    return found[:MAX_LINKS]


def _system(prompt: str, pack: AppPack, state: ChatState, *, guide=False, language=False) -> str:
    parts = [prompt, f"App: {pack.name}"]
    if guide:
        parts.append(f"The app's guide:\n{pack.guide.strip()}")
    if language:
        ctx = state.get("context")
        parts.append(f"Language: {language_tag(ctx.language if ctx else '')}")
    return "\n\n".join(parts)


def _material(state: ChatState, *, facts=False, context=False, kept=False) -> str:
    parts = ["Conversation so far, oldest first:"]
    for m in state.get("messages", [])[-MAX_MESSAGES:]:
        text = nfc(_content(m)).strip()
        if text:
            parts.append(data("user" if isinstance(m, HumanMessage) else "assistant", text))
    readings = state.get("readings", [])
    if readings:
        parts.append("Files and links the user shared:")
        for r in readings:
            text = r.text + (f"\nUnsure (not facts): {'; '.join(r.unsure)}" if r.unsure else "")
            parts.append(data(r.kind, text, file_id=r.file_id))
    ctx = state.get("context")
    if context and ctx is not None:
        recent = "\n".join(f"- {p}" for p in [*ctx.recent_posts, *state.get("past_pieces", [])])
        about = (
            f"Voice: {ctx.voice}\nTopics: {', '.join(ctx.topics)}\nPatterns that work: {'; '.join(ctx.patterns)}\n"
            f"Recent posts and pieces (do not repeat):\n{recent}"
        )
        parts.append("About the app's user:\n" + data("context", about))
    if kept and _kept(state):
        blocks = "\n".join(data("kept", k.text, id=k.id) for k in _kept(state))
        parts.append("Facts the user kept in an earlier chat (their own words, not used yet):\n" + blocks)
    if facts:
        parts.append(fact_blocks(state.get("facts", [])))
    return "\n\n".join(parts)


def _spec_lines(specs: list[tuple[WriteSpec, int]]) -> str:
    lines = []
    for spec, count in specs:
        rules = [f"at most {spec.max_chars} characters"] if spec.max_chars else []
        rules += ["no links"] if spec.no_links else []
        rules += ["no long dashes (— or –)"] if spec.no_dashes else []
        rules += ["may carry an image"] if spec.may_carry_image else ["no image"]
        lines.append(
            f'- kind "{spec.kind}": exactly {count} piece(s). Rules: {"; ".join(rules)}.\n'
            f"  Instructions: {spec.instructions.strip()}"
        )
    return "What to write:\n" + "\n".join(lines)


# ---- nodes ----


async def read_files(state: ChatState, t: Turn) -> dict:
    """Starts the turn: resets its work, reads this message's files and up to 2 links in its text."""
    t.cost = 0.0
    update: dict = {
        "file_ids": [],
        "pieces": [],
        "problems": {},
        "passed": [],
        "rewrites": 0,
        "capped": False,
        "ready": False,
        "just_write": False,
        "ask_hint": None,
    }
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
    return {"context": ctx, "context_at": _now()}


async def interview(state: ChatState, t: Turn) -> dict:
    """Is there enough to write? After 2 ask-back rounds there is, by rule."""
    await t.emit("status", {"step": "thinking"})
    if state.get("rounds_asked", 0) >= MAX_ROUNDS:
        return {"ready": True}
    system = _system(prompts.INTERVIEW, t.pack, state, guide=True)
    out = await t.json(system, _material(state, facts=True, kept=True), InterviewOut, max_tokens=300)
    return {"ready": out.enough or out.just_write, "just_write": out.just_write, "ask_hint": out.question}


async def ask_back(state: ChatState, t: Turn) -> dict:
    """Streams one short question and ends the turn. The thread's first one offers the facts the user kept earlier
    instead, in fixed words (no model call); that offer is not one of the ask-back rounds."""
    kept = _kept(state)
    if kept and not state.get("kept_offered") and not state.get("facts"):
        offer = "\n".join([KEPT_OFFER, *(f"- {' '.join(k.text.split())}" for k in kept)])
        await t.emit("text", {"delta": offer})
        await t.note(KEPT_NOTE)
        return {"messages": [AIMessage(content=offer)], "kept_offered": True}
    system = _system(prompts.ASK_BACK, t.pack, state, guide=True, language=True)
    hint = state.get("ask_hint")
    user = _material(state, facts=True, kept=True) + (f"\n\nAsk about: {data('assistant', hint)}" if hint else "")
    question = nfc(await t.stream(system, user, max_tokens=200)).strip()
    await t.note("Asked back for details")
    return {"messages": [AIMessage(content=question)], "rounds_asked": state.get("rounds_asked", 0) + 1}


async def extract_facts(state: ChatState, t: Turn) -> dict:
    """Adds the new facts in the conversation and files to the thread's facts (deduplicated by text)."""
    system = _system(prompts.EXTRACT_FACTS, t.pack, state)
    out = await t.json(system, _material(state, facts=True, kept=True), ExtractOut, max_tokens=1500)
    readings = {r.file_id: r for r in state.get("readings", [])}
    said = [Fact(numbers=numbers_in(text)) for text in _source_texts(state)]
    # A kept fact is stored word for word with its own id: the app marks it used by that text when a post tells it.
    kept = {_kept_key(k.text): k for k in _kept(state)}
    known = list(state.get("facts", []))
    keys = {_kept_key(f.text) for f in known}
    for item in out.facts:
        text = nfc(item.text).strip()
        was_kept = kept.get(_kept_key(text))
        if was_kept is not None:
            text = was_kept.text
        if not text or _kept_key(text) in keys:
            continue
        keys.add(_kept_key(text))
        if unsupported_numbers(text, said):
            continue  # a number the user never said or showed: the extractor made it up
        if was_kept is not None:
            known.append(Fact(id=was_kept.id, text=text, numbers=numbers_in(text)))
            continue
        reading = readings.get(item.file_id) if item.file_id else None
        source = reading.kind if reading else "typed"
        file_id = reading.file_id if reading else None
        known.append(Fact(id=new_id(), text=text, numbers=numbers_in(text), source=source, file_id=file_id))
    return {"facts": known}


async def write(state: ChatState, t: Turn) -> dict:
    """Writes every piece the pack asks for; on the second pass, rewrites only the failing ones."""
    await t.emit("status", {"step": "writing"})
    # The old pipeline writes single posts only; threads arrive with the conversation-first bot.
    specs = {w.kind: w for w in t.pack.writes if w.kind != "x_thread"}
    facts = state.get("facts", [])
    labels = {f"F{i}": f.id for i, f in enumerate(facts, 1)} | {f.id: f.id for f in facts}
    system = _system(prompts.WRITER, t.pack, state, language=True)
    material = _material(state, context=True, facts=True)
    problems = state.get("problems", {})
    pieces = list(state.get("pieces", []))
    failing = [p for p in pieces if p.id in problems]

    def fact_ids(out: PieceOut) -> list[str]:
        return list(dict.fromkeys(labels[x] for x in out.fact_ids if x in labels))

    if not failing:
        out = await t.json(
            system + "\n\n" + _spec_lines([(w, w.count) for w in t.pack.writes if w.kind != "x_thread"]),
            material,
            WriterOut,
            3000,
        )
        counts: Counter[str] = Counter()
        for p in out.pieces:
            spec = specs.get(p.spec_kind)
            text = nfc(p.text).strip()
            if spec is None or not text or counts[spec.kind] >= spec.count:
                continue
            counts[spec.kind] += 1
            pieces.append(
                Piece(id=new_id(), spec_kind=spec.kind, text=text, fact_ids=fact_ids(p), image_file_id=p.image_file_id)
            )
        if not pieces:
            await t.emit("text", {"delta": NOTHING_WRITTEN})
            return {"pieces": [], "rounds_asked": 0, "just_write": False}
        return {"pieces": pieces}

    asks = [f"Rewrite these {len(failing)} piece(s), in this order, fixing the problems listed:"]
    for n, p in enumerate(failing, 1):
        asks.append(f'{n}. kind "{p.spec_kind}"; problems: {"; ".join(problems[p.id])}\n' + data("draft", p.text))
    kinds = Counter(p.spec_kind for p in failing)
    spec_system = system + "\n\n" + _spec_lines([(specs[k], c) for k, c in kinds.items()])
    out = await t.json(spec_system, material + "\n\n" + "\n".join(asks), WriterOut, 3000)
    by_kind: dict[str, list[PieceOut]] = {}
    for o in out.pieces:
        if nfc(o.text).strip():
            by_kind.setdefault(o.spec_kind, []).append(o)
    for i, p in enumerate(pieces):
        # Rewrites pair with failing pieces of the same kind, in order. One with no match keeps its old text,
        # so it fails again and is dropped.
        if p.id in problems and by_kind.get(p.spec_kind):
            o = by_kind[p.spec_kind].pop(0)
            pieces[i] = p.model_copy(
                update={"text": nfc(o.text).strip(), "fact_ids": fact_ids(o), "image_file_id": o.image_file_id}
            )
            await t.note("Rewrote a post that had a problem")
    return {"pieces": pieces, "rewrites": 1}


async def check(state: ChatState, t: Turn) -> dict:
    """Numbers, the app's rules, repeats, then a cheap claim check. Failing pieces go to `problems`."""
    await t.emit("status", {"step": "checking"})
    # The old pipeline writes single posts only; threads arrive with the conversation-first bot.
    specs = {w.kind: w for w in t.pack.writes if w.kind != "x_thread"}
    facts = state.get("facts", [])
    second = state.get("rewrites", 0) > 0
    old = state.get("problems", {})
    pieces = list(state.get("pieces", []))
    passed = list(state.get("passed", []))
    ctx = state.get("context")
    recent = [*(ctx.recent_posts if ctx else []), *state.get("past_pieces", [])]
    problems: dict[str, list[str]] = {}
    capped = False
    try:
        for i, p in enumerate(pieces):
            if p.id in passed or (second and p.id not in old):
                continue
            spec = specs[p.spec_kind]
            readings = state.get("readings", [])
            if p.image_file_id is not None and not image_allowed(p.image_file_id, p.fact_ids, spec, readings, facts):
                p = pieces[i] = p.model_copy(update={"image_file_id": None})
            reasons = post_problems(p.text, spec, facts, recent + [q.text for q in pieces if q.id in passed])
            if not reasons:
                system, user = claim_request(t.pack, facts, [p.text])
                if (await t.json(system, user, ClaimOut, max_tokens=CLAIM_TOKENS, cheap=True)).unsupported:
                    reasons = [CLAIM]
            if reasons:
                problems[p.id] = reasons
            else:
                passed.append(p.id)
    except Capped:
        await t.emit("text", {"delta": CAP_TEXT})
        capped = True
    if second:  # a rewritten piece the cap left unchecked keeps its first reasons
        problems = {**{k: v for k, v in old.items() if k not in passed}, **problems}
    return {"pieces": pieces, "problems": problems, "passed": passed, "capped": capped}


MAX_CARD_FACTS = 30  # the API refuses to keep more than this many facts at once


async def present(state: ChatState, t: Turn) -> dict:
    """Shows the pieces that passed and the ones dropped (with why), with the facts; nothing unchecked."""
    passed = set(state.get("passed", []))
    problems = state.get("problems", {})
    shown: list[Piece] = []
    for p in state.get("pieces", []):
        if p.id in passed:
            shown.append(p)
        elif p.id in problems:
            shown.append(p.model_copy(update={"dropped": problems[p.id][0]}))
    if not shown:
        return {"pieces": []}
    kept = [p for p in shown if p.dropped is None]
    for p in shown:
        if p.dropped:
            await t.note(f"Dropped a post: {p.dropped}")
    if kept:
        await t.note("Checked: every number is from you")
    # Only the facts the shown pieces use, in first-use order: "Keep for next week" sends them back, and the server
    # takes at most MAX_CARD_FACTS.
    by_id = {f.id: f for f in state.get("facts", [])}
    used = list(dict.fromkeys(i for p in shown for i in p.fact_ids if i in by_id))[:MAX_CARD_FACTS]
    card = {"pieces": [p.model_dump() for p in shown], "facts": [by_id[i].model_dump() for i in used]}
    await t.emit("text", {"delta": INTRO})
    await t.emit("card", card)
    return {
        "pieces": shown,
        "problems": {},
        "messages": [AIMessage(content="\n\n".join(p.text for p in kept), additional_kwargs={"card": card})],
        "past_pieces": [*state.get("past_pieces", []), *(p.text for p in kept)],
        "rounds_asked": 0,
        "just_write": False,
    }
