"""The graph's nodes, one async function each: `(state, turn) -> state update`.

The graph has no free tool calling: the only app tool it reaches is `context()`. Everything a user, a file, a link
or the app supplied goes to a model inside a `<data>` block. Every model call first checks the message's cost cap.
"""

import re
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict

from assistant.checks import Fact, numbers_in, repeats, rule_problems, unsupported_numbers
from assistant.db import new_id
from assistant.files.intake import UploadError
from assistant.files.read import FileReading
from assistant.graph import prompts
from assistant.graph.prompts import data
from assistant.graph.state import ChatState, Piece
from assistant.llm import Llm, LlmError
from assistant.packs import AppPack, WriteSpec
from assistant.tools import AppTools

CAP_TEXT = "That's as far as I can go on this message. Send another to continue."
CONTEXT_TTL = 300  # seconds the app's context is reused within a thread
MAX_ROUNDS = 2  # ask-back rounds before writing with what there is
MAX_LINKS = 2  # links read from one message
MAX_MESSAGES = 20  # conversation messages shown to a model

NUMBER = "a number you didn't give ({})"
RULES = {"too_long": "too long for this app", "has_link": "it had a link", "has_dash": "it had a long dash"}
REPEAT = "it repeats a recent post"
CLAIM = "it says something you didn't tell me"
READ_NOTES = {
    "voice": "Read your voice note",
    "image": "Read your screenshot",
    "pdf": "Read your PDF",
    "link": "Read the link",
}

_LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
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


class ClaimOut(_Strict):
    unsupported: list[str]


# ---- the turn: dependencies, spending and events ----


class Capped(Exception):
    """The message's cost cap was reached before a model call."""


@dataclass
class Deps:
    pack: AppPack
    llm: Llm
    tools_factory: Callable[[], AppTools]
    on_event: Callable[[str, dict], Awaitable[None]]
    read_files: Callable[[list[str]], Awaitable[list[FileReading]]]
    read_link: Callable[[str], Awaitable[FileReading]]


class Turn:
    """A node's view of the turn: its dependencies and everything the turn has spent so far."""

    def __init__(self, deps: Deps, state: ChatState):
        self.deps = deps
        self.pack = deps.pack
        self.cost: float = state.get("cost_usd", 0.0)

    async def emit(self, kind: str, payload: dict) -> None:
        await self.deps.on_event(kind, payload)

    async def note(self, text: str) -> None:
        await self.emit("note", {"text": text})

    def gate(self) -> None:
        if self.cost >= self.pack.message_usd_cap:
            raise Capped

    async def json[T: BaseModel](self, system: str, user: str, schema: type[T], max_tokens: int, cheap=False) -> T:
        self.gate()
        models = self.pack.models
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            out, cost = await self.deps.llm.chat_json(
                messages, models.cheap if cheap else models.main, models.fallbacks, schema, max_tokens
            )
        except LlmError as e:
            self.cost += e.cost_usd
            raise
        self.cost += cost
        assert isinstance(out, schema)
        return out

    async def stream(self, system: str, user: str, max_tokens: int) -> str:
        self.gate()
        models = self.pack.models
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def forward(delta: str) -> None:
            await self.emit("text", {"delta": delta})

        try:
            result = await self.deps.llm.chat(messages, models.main, models.fallbacks, max_tokens, on_text=forward)
        except LlmError as e:
            self.cost += e.cost_usd
            raise
        self.cost += result.cost_usd
        return result.text


def bind(node: Callable[[ChatState, Turn], Awaitable[dict]], deps: Deps) -> Callable[[ChatState], Awaitable[dict]]:
    """A LangGraph node: runs `node`, keeps the turn's cost in state, and ends the turn at the cost cap.

    An error leaving the graph carries `cost_usd`: everything this turn spent, so the caller can still charge it.
    """

    async def run(state: ChatState) -> dict:
        turn = Turn(deps, state)
        try:
            update = await node(state, turn)
        except Capped:
            await turn.emit("text", {"delta": CAP_TEXT})
            return {"capped": True, "cost_usd": turn.cost}
        except Exception as e:
            e.cost_usd = turn.cost  # type: ignore[attr-defined]
            raise
        return {**update, "cost_usd": turn.cost}

    run.__name__ = node.__name__
    return run


# ---- helpers ----


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _key(text: str) -> str:
    return " ".join(nfc(text).casefold().split())


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


def _fmt(n: float) -> str:
    return format(n, "f").rstrip("0").rstrip(".")


def _system(prompt: str, pack: AppPack, state: ChatState, *, guide=False, language=False) -> str:
    parts = [prompt, f"App: {pack.name}"]
    if guide:
        parts.append(f"The app's guide:\n{pack.interview_guide.strip()}")
    if language:
        ctx = state.get("context")
        parts.append(f"Language: {(ctx.language if ctx else 'en')[:20]}")
    return "\n\n".join(parts)


def _fact_blocks(facts: list[Fact]) -> str:
    if not facts:
        return "Known facts: none yet."
    lines = [data("user" if f.source == "typed" else f.source, f.text, fact=f"F{i}") for i, f in enumerate(facts, 1)]
    return "Known facts:\n" + "\n".join(lines)


def _material(state: ChatState, *, facts=False, context=False) -> str:
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
    if facts:
        parts.append(_fact_blocks(state.get("facts", [])))
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


def _problems(piece: Piece, spec: WriteSpec, facts: list[Fact], recent: list[str]) -> list[str]:
    reasons = []
    missing = unsupported_numbers(piece.text, facts)
    if missing:
        reasons.append(NUMBER.format(_fmt(missing[0])))
    reasons += [RULES[code] for code in rule_problems(piece.text, spec)]
    if repeats(piece.text, recent):
        reasons.append(REPEAT)
    return reasons


def _image_allowed(piece: Piece, spec: WriteSpec, state: ChatState) -> bool:
    images = {r.file_id for r in state.get("readings", []) if r.kind == "image"}
    from_image = {f.id for f in state.get("facts", []) if f.file_id == piece.image_file_id}
    return spec.may_carry_image and piece.image_file_id in images and bool(from_image & set(piece.fact_ids))


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
            try:
                got = [await t.deps.read_link(ref)] if is_link else await t.deps.read_files([ref])
            except UploadError as e:
                t.cost += e.cost_usd
                await t.emit("text", {"delta": e.message + "\n"})
                continue
            except LlmError as e:
                t.cost += e.cost_usd
                raise
            for r in got:
                t.cost += r.cost_usd
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
    ctx = ctx.model_copy(update={"recent_posts": [nfc(p) for p in ctx.recent_posts]})
    return {"context": ctx, "context_at": _now()}


async def interview(state: ChatState, t: Turn) -> dict:
    """Is there enough to write? After 2 ask-back rounds there is, by rule."""
    if state.get("rounds_asked", 0) >= MAX_ROUNDS:
        return {"ready": True}
    system = _system(prompts.INTERVIEW, t.pack, state, guide=True)
    out = await t.json(system, _material(state, facts=True), InterviewOut, max_tokens=300)
    return {"ready": out.enough or out.just_write, "just_write": out.just_write, "ask_hint": out.question}


async def ask_back(state: ChatState, t: Turn) -> dict:
    """Streams one short question and ends the turn."""
    system = _system(prompts.ASK_BACK, t.pack, state, guide=True, language=True)
    hint = state.get("ask_hint")
    user = _material(state, facts=True) + (f"\n\nAsk about: {hint}" if hint else "")
    question = nfc(await t.stream(system, user, max_tokens=200)).strip()
    await t.note("Asked back for details")
    return {"messages": [AIMessage(content=question)], "rounds_asked": state.get("rounds_asked", 0) + 1}


async def extract_facts(state: ChatState, t: Turn) -> dict:
    """Adds the new facts in the conversation and files to the thread's facts (deduplicated by text)."""
    system = _system(prompts.EXTRACT_FACTS, t.pack, state)
    out = await t.json(system, _material(state, facts=True), ExtractOut, max_tokens=1500)
    readings = {r.file_id: r for r in state.get("readings", [])}
    known = list(state.get("facts", []))
    keys = {_key(f.text) for f in known}
    for item in out.facts:
        text = nfc(item.text).strip()
        if not text or _key(text) in keys:
            continue
        keys.add(_key(text))
        reading = readings.get(item.file_id) if item.file_id else None
        source = reading.kind if reading else "typed"
        file_id = reading.file_id if reading else None
        known.append(Fact(id=new_id(), text=text, numbers=numbers_in(text), source=source, file_id=file_id))
    return {"facts": known}


async def write(state: ChatState, t: Turn) -> dict:
    """Writes every piece the pack asks for; on the second pass, rewrites only the failing ones."""
    specs = {w.kind: w for w in t.pack.writes}
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
            system + "\n\n" + _spec_lines([(w, w.count) for w in t.pack.writes]), material, WriterOut, 3000
        )
        counts: Counter[str] = Counter()
        for p in out.pieces:
            spec = specs.get(p.spec_kind)
            if spec is None or counts[spec.kind] >= spec.count:
                continue
            counts[spec.kind] += 1
            text = nfc(p.text).strip()
            pieces.append(
                Piece(id=new_id(), spec_kind=spec.kind, text=text, fact_ids=fact_ids(p), image_file_id=p.image_file_id)
            )
        return {"pieces": pieces}

    asks = [f"Rewrite these {len(failing)} piece(s), in this order, fixing the problems listed:"]
    for n, p in enumerate(failing, 1):
        asks.append(f'{n}. kind "{p.spec_kind}"; problems: {"; ".join(problems[p.id])}\n' + data("draft", p.text))
    kinds = Counter(p.spec_kind for p in failing)
    spec_system = system + "\n\n" + _spec_lines([(specs[k], c) for k, c in kinds.items()])
    out = await t.json(spec_system, material + "\n\n" + "\n".join(asks), WriterOut, 3000)
    redone = {p.id: o for p, o in zip(failing, out.pieces, strict=False)}
    for i, p in enumerate(pieces):
        if p.id in redone:
            o = redone[p.id]
            pieces[i] = p.model_copy(
                update={"text": nfc(o.text).strip(), "fact_ids": fact_ids(o), "image_file_id": o.image_file_id}
            )
    for _ in failing:
        await t.note("Rewrote a post that had a problem")
    return {"pieces": pieces, "rewrites": 1}


async def check(state: ChatState, t: Turn) -> dict:
    """Numbers, the app's rules, repeats, then a cheap claim check. Failing pieces go to `problems`."""
    specs = {w.kind: w for w in t.pack.writes}
    facts = state.get("facts", [])
    second = state.get("rewrites", 0) > 0
    old = state.get("problems", {})
    pieces = list(state.get("pieces", []))
    passed = list(state.get("passed", []))
    ctx = state.get("context")
    recent = [*(ctx.recent_posts if ctx else []), *state.get("past_pieces", [])]
    problems: dict[str, list[str]] = {}
    capped = False
    claim_system = _system(prompts.CLAIM_CHECK, t.pack, state)
    try:
        for i, p in enumerate(pieces):
            if p.id in passed or (second and p.id not in old):
                continue
            spec = specs[p.spec_kind]
            if p.image_file_id is not None and not _image_allowed(p, spec, state):
                p = pieces[i] = p.model_copy(update={"image_file_id": None})
            reasons = _problems(p, spec, facts, recent + [q.text for q in pieces if q.id in passed])
            if not reasons:
                user = _fact_blocks(facts) + "\n\nThe piece to check:\n" + data("draft", p.text)
                if (await t.json(claim_system, user, ClaimOut, max_tokens=400, cheap=True)).unsupported:
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
    card = {"pieces": [p.model_dump() for p in shown], "facts": [f.model_dump() for f in state.get("facts", [])]}
    await t.emit("card", card)
    return {
        "pieces": shown,
        "problems": {},
        "messages": [AIMessage(content="\n\n".join(p.text for p in kept), additional_kwargs={"card": card})],
        "past_pieces": [*state.get("past_pieces", []), *(p.text for p in kept)],
        "rounds_asked": 0,
        "just_write": False,
    }
