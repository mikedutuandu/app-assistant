"""The drafting tools the conversation model calls: `write_drafts` and `revise_draft`, with quiet checks.

The writer model (the pack's `writer`, with thinking when `writer_reasoning` is "low") writes posts and threads
from the user's facts. Every post of every draft is then checked: numbers only from the facts, the pack's rules,
repeats (each post, and a thread as a whole), then a claim check on the cheap model. A failing draft is rewritten
once with its problems; one that still fails is dropped, and the result's `note` says why in one short line for the
conversation model (the user never sees check reasons). Every model call is charged through `WriterDeps.spend`,
failed ones included. Everything the user, their files, their app or the conversation model supplied reaches the
writer inside `<data>` blocks.

The old pipeline (`graph/nodes.py`) uses the check helpers here until the conversation agent replaces it.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from assistant.checks import Fact, nfc, repeats, rule_problems, unsupported_numbers
from assistant.db import new_id
from assistant.files.read import FileReading
from assistant.graph import prompts
from assistant.graph.prompts import data
from assistant.llm import Llm, LlmError
from assistant.packs import AppPack, WriteSpec
from assistant.tools import AppContext

KINDS = ("x_post", "x_thread")
MAX_DRAFTS = 3  # drafts in one call
THREAD_POSTS = 8  # a thread's most posts when its spec doesn't say
WRITER_TOKENS = 3000
THREAD_TOKENS = 5000  # up to 3 threads of 8 posts of 280 characters
THINKING_TOKENS = 1024  # added to the writer's max_tokens when it thinks
CLAIM_TOKENS = 400

# Reasons a draft fails, in the user's terms (the old pipeline shows them; the tools pass them to the model).
NUMBER = "a number you didn't give ({})"
RULES = {"too_long": "too long for this app", "has_link": "it had a link", "has_dash": "it had a long dash"}
REPEAT = "it repeats a recent post"
CLAIM = "it says something you didn't tell me"
TOO_MANY = "too many posts for one thread (at most {})"
TOO_FEW = "a thread needs at least two posts"
UNCHECKED = "it couldn't be checked"

# Notes for the conversation model.
NO_FACTS = "no facts yet"
WRITER_FAILED = "the writer failed, nothing was written; it may work if tried again"
NOTHING = "the writer returned nothing usable"

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")


# ---- the tools' types ----


class Draft(BaseModel):
    id: str
    kind: Literal["x_post", "x_thread"]
    posts: list[str]  # one item for a post
    image_file_id: str | None = None
    fact_ids: list[str] = []


class DraftResult(BaseModel):
    drafts: list[Draft]
    note: str | None = None  # one short line about anything dropped or refused, for the conversation model
    cost_usd: float = 0.0


@dataclass
class WriterDeps:
    pack: AppPack
    llm: Llm
    facts: list[Fact]
    readings: list[FileReading]
    context: AppContext | None
    recent: list[str]  # this thread's earlier drafts' posts and the context's recent posts
    spend: Callable[[float], Awaitable[None]]  # charges one finished model call
    status: Callable[[str], Awaitable[None]]  # "writing", "checking"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftOut(_Strict):
    posts: list[str]
    fact_ids: list[str]
    image_file_id: str | None


class DraftsOut(_Strict):
    drafts: list[DraftOut]


class ClaimOut(_Strict):
    unsupported: list[str]


# ---- shared helpers (the old pipeline uses these too) ----


def fmt(n: float) -> str:
    return format(n, "f").rstrip("0").rstrip(".")


def language_tag(tag: str) -> str:
    """The app's language tag (en, vi, pt-BR), or "en" for anything else: it goes into a system prompt."""
    return tag if _LANGUAGE_TAG.fullmatch(tag) else "en"


def fact_blocks(facts: list[Fact]) -> str:
    if not facts:
        return "Known facts: none yet."
    lines = [data("user" if f.source == "typed" else f.source, f.text, fact=f"F{i}") for i, f in enumerate(facts, 1)]
    return "Known facts:\n" + "\n".join(lines)


def post_problems(text: str, spec: WriteSpec, facts: list[Fact], recent: list[str]) -> list[str]:
    """Numbers, the pack's rules and repeats for one post: no model call."""
    reasons = []
    missing = unsupported_numbers(text, facts)
    if missing:
        reasons.append(NUMBER.format(fmt(missing[0])))
    reasons += [RULES[code] for code in rule_problems(text, spec)]
    if repeats(text, recent):
        reasons.append(REPEAT)
    return reasons


def image_allowed(
    image_file_id: str | None, fact_ids: list[str], spec: WriteSpec, readings: list[FileReading], facts: list[Fact]
) -> bool:
    """A draft may carry an image only if its kind allows one, the user shared it, and it uses a fact read from it."""
    images = {r.file_id for r in readings if r.kind == "image"}
    from_image = {f.id for f in facts if f.file_id == image_file_id}
    return spec.may_carry_image and image_file_id in images and bool(from_image & set(fact_ids))


def _posts_blocks(posts: list[str], source: str = "draft") -> str:
    if len(posts) == 1:
        return data(source, posts[0])
    return "\n".join(data(source, p, post=str(i)) for i, p in enumerate(posts, 1))


def claim_request(pack: AppPack, facts: list[Fact], posts: list[str]) -> tuple[str, str]:
    """The claim check's system and user prompts for one piece: a post, or a thread's posts in order."""
    system = f"{prompts.CLAIM_CHECK}\n\nApp: {pack.name}"
    what = "The piece to check:" if len(posts) == 1 else "The thread to check, post by post:"
    return system, f"{fact_blocks(facts)}\n\n{what}\n{_posts_blocks(posts)}"


# ---- the tools ----


async def write_drafts(w: WriterDeps, brief: str, kind: str, count: int) -> DraftResult:
    """Writes `count` (1 to 3) checked drafts of `kind` about `brief` from the user's facts."""
    if not w.facts:
        return DraftResult(drafts=[], note=NO_FACTS)
    spec = _spec(w.pack, kind)
    if spec is None:
        return DraftResult(drafts=[], note=_unknown(w.pack, kind))
    count = max(1, min(MAX_DRAFTS, count))
    ask = (
        f'Write exactly {count} draft(s) of kind "{spec.kind}" about what this brief describes, '
        f"from the known facts only:\n{data('brief', nfc(brief))}"
    )
    return await _Run(w, spec, w.recent).draft(ask, count)


async def revise_draft(w: WriterDeps, draft: Draft, instruction: str) -> DraftResult:
    """One new checked draft: `draft` changed as `instruction` asks ("shorter", "another angle", ...)."""
    if not w.facts:
        return DraftResult(drafts=[], note=NO_FACTS)
    spec = _spec(w.pack, draft.kind)
    if spec is None:
        return DraftResult(drafts=[], note=_unknown(w.pack, draft.kind))
    ask = (
        f'Revise this draft of kind "{spec.kind}" as the user asks, and reply with exactly 1 draft.\n'
        f"The draft:\n{_posts_blocks(draft.posts)}\n"
        + _image_line(w, draft)
        + f"What the user asks:\n{data('instruction', nfc(instruction))}"
    )
    # The draft being revised isn't a repeat of itself.
    own = {_key(p) for p in draft.posts}
    recent = [r for r in w.recent if _key(r) not in own]
    return await _Run(w, spec, recent).draft(ask, 1)


# ---- how a call runs ----


def _image_line(w: WriterDeps, draft: Draft) -> str:
    """Names the draft's image to the writer only if the user shared it, and only as an attribute of a data block."""
    shared = {r.file_id for r in w.readings if r.kind == "image"}
    if draft.image_file_id is None or draft.image_file_id not in shared:
        return ""
    return f"It carries this image, keep it:\n{data('image', '', file_id=draft.image_file_id)}\n"


def _key(text: str) -> str:
    return " ".join(nfc(text).casefold().split())


def _spec(pack: AppPack, kind: str) -> WriteSpec | None:
    if kind not in KINDS:
        return None
    return next((s for s in pack.writes if s.kind == kind), None)


def _unknown(pack: AppPack, kind: str) -> str:
    kinds = ", ".join(s.kind for s in pack.writes if s.kind in KINDS) or "none"
    return f"can't write {kind!r} here; kinds: {kinds}"


def _max_posts(spec: WriteSpec) -> int:
    return spec.max_posts or THREAD_POSTS


def _spec_rules(spec: WriteSpec) -> str:
    thread = spec.kind == "x_thread"
    rules = [f"2 to {_max_posts(spec)} posts"] if thread else ["one post"]
    if spec.max_chars:
        rules.append(f"at most {spec.max_chars} characters" + (" per post" if thread else ""))
    rules += ["no links"] if spec.no_links else []
    rules += ["no long dashes (— or –)"] if spec.no_dashes else []
    rules += ["may carry an image"] if spec.may_carry_image else ["no image"]
    return f'Kind "{spec.kind}". Rules: {"; ".join(rules)}.\nInstructions: {spec.instructions.strip()}'


def _plural(n: int) -> str:
    return "one draft" if n == 1 else f"{n} drafts"


@dataclass
class _Problem:
    reason: str
    post: int | None = None  # 1-based, for a thread's post
    details: list[str] = field(default_factory=list)  # for the writer only: what a claim check found unsupported

    def for_writer(self) -> str:
        # The fragments quote the draft (the user's words, files, links): each goes in its own data block.
        text = f"{self.reason}: " + " ".join(data("claim", d) for d in self.details) if self.details else self.reason
        return f"post {self.post}: {text}" if self.post else text


@dataclass
class _Run:
    w: WriterDeps
    spec: WriteSpec
    recent: list[str]
    cost: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        facts = self.w.facts
        self.labels = {f"F{i}": f.id for i, f in enumerate(facts, 1)} | {f.id: f.id for f in facts}

    async def _json[T: BaseModel](
        self, system: str, user: str, schema: type[T], model: str, max_tokens: int, reasoning: str | None
    ) -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            out, cost = await self.w.llm.chat_json(
                messages, model, self.w.pack.models.fallbacks, schema, max_tokens, reasoning=reasoning
            )
        except LlmError as e:
            await self._spend(e.cost_usd)
            raise
        await self._spend(cost)
        if not isinstance(out, schema):
            raise LlmError("bad_output", f"expected {schema.__name__}")
        return out

    async def _spend(self, cost: float) -> None:
        self.cost += cost
        await self.w.spend(cost)

    def _result(self, drafts: list[Draft], note: str | None) -> DraftResult:
        return DraftResult(drafts=drafts, note=note, cost_usd=self.cost)

    # -- writing --

    def _system(self) -> str:
        ctx = self.w.context
        return "\n\n".join(
            [prompts.DRAFT_WRITER, f"App: {self.w.pack.name}", f"Language: {language_tag(ctx.language if ctx else '')}"]
        )

    def _material(self) -> str:
        parts = []
        ctx = self.w.context
        recent = "\n".join(f"- {p}" for p in self.recent) or "none"
        if ctx is not None:
            about = (
                f"Voice: {ctx.voice}\nTopics: {', '.join(ctx.topics)}\nPatterns that work: {'; '.join(ctx.patterns)}\n"
                f"Recent posts (do not repeat):\n{recent}"
            )
        else:
            about = f"Recent posts (do not repeat):\n{recent}"
        parts.append("About the app's user:\n" + data("context", about))
        if self.w.readings:
            blocks = []
            for r in self.w.readings:
                text = r.text + (f"\nUnsure (not facts): {'; '.join(r.unsure)}" if r.unsure else "")
                blocks.append(data(r.kind, text, file_id=r.file_id))
            parts.append("Files and links the user shared:\n" + "\n".join(blocks))
        parts.append(fact_blocks(self.w.facts))
        parts.append(_spec_rules(self.spec))
        return "\n\n".join(parts)

    async def _write(self, ask: str) -> list[DraftOut] | None:
        """One writer call; None when it failed (its cost is charged)."""
        await self.w.status("writing")
        models = self.w.pack.models
        thinking = models.writer_reasoning == "low"
        try:
            out = await self._json(
                self._system(),
                f"{self._material()}\n\n{ask}",
                DraftsOut,
                models.writer,
                (THREAD_TOKENS if self.spec.kind == "x_thread" else WRITER_TOKENS)
                + (THINKING_TOKENS if thinking else 0),
                "low" if thinking else None,
            )
        except LlmError:
            return None
        return out.drafts

    def _draft(self, out: DraftOut, id: str | None = None) -> Draft | None:
        posts = [p for p in (nfc(x).strip() for x in out.posts) if p]
        if not posts:
            return None
        if self.spec.kind == "x_post" and len(posts) > 1:
            posts = ["\n\n".join(posts)]  # a post is one text: the length rule judges it whole
        fact_ids = list(dict.fromkeys(self.labels[x] for x in out.fact_ids if x in self.labels))
        image = out.image_file_id
        if image is not None and not image_allowed(image, fact_ids, self.spec, self.w.readings, self.w.facts):
            image = None
        return Draft(id=id or new_id(), kind=self.spec.kind, posts=posts, image_file_id=image, fact_ids=fact_ids)

    # -- checking --

    def _local(self, d: Draft, seen: list[str]) -> list[_Problem]:
        """Every check that needs no model, on every post, and the thread as a whole for repeats."""
        problems: list[_Problem] = []
        thread = d.kind == "x_thread"
        if thread and len(d.posts) > _max_posts(self.spec):
            problems.append(_Problem(TOO_MANY.format(_max_posts(self.spec))))
        if thread and len(d.posts) < 2:
            problems.append(_Problem(TOO_FEW))
        earlier: list[str] = []
        for i, post in enumerate(d.posts, 1):
            reasons = post_problems(post, self.spec, self.w.facts, seen + earlier)
            problems += [_Problem(r, i if thread else None) for r in reasons]
            earlier.append(post)
        if thread and not any(p.reason == REPEAT for p in problems) and repeats(" ".join(d.posts), seen):
            problems.append(_Problem(REPEAT))
        return problems

    async def _claims(self, d: Draft) -> list[_Problem]:
        system, user = claim_request(self.w.pack, self.w.facts, d.posts)
        try:
            out = await self._json(system, user, ClaimOut, self.w.pack.models.cheap, CLAIM_TOKENS, None)
        except LlmError:
            return [_Problem(UNCHECKED)]  # never shown unchecked
        if out.unsupported:
            return [_Problem(CLAIM, details=[nfc(u).strip()[:300] for u in out.unsupported[:5]])]
        return []

    async def _check(self, drafts: list[Draft], seen: list[str]) -> dict[str, list[_Problem]]:
        """Checks each draft; a passing one joins `seen`, so later drafts can't repeat it. Returns the failures."""
        await self.w.status("checking")
        failed: dict[str, list[_Problem]] = {}
        for d in drafts:
            problems = self._local(d, seen) or await self._claims(d)
            if problems:
                failed[d.id] = problems
            else:
                seen.extend(d.posts)
        return failed

    # -- the whole run --

    async def draft(self, ask: str, count: int) -> DraftResult:
        outs = await self._write(ask)
        if outs is None:
            return self._result([], WRITER_FAILED)
        drafts = [d for d in (self._draft(o) for o in outs) if d is not None][:count]
        if not drafts:
            return self._result([], NOTHING)
        seen = list(self.recent)
        failed = await self._check(drafts, seen)
        if failed:
            failing = [d for d in drafts if d.id in failed]
            lines = [f"Rewrite these {len(failing)} draft(s), in this order, fixing the problems listed:"]
            for n, d in enumerate(failing, 1):
                listed = "; ".join(p.for_writer() for p in failed[d.id])
                lines.append(f"{n}. problems: {listed}\n{_posts_blocks(d.posts)}")
            outs = await self._write(f"{ask}\n\n" + "\n".join(lines)) or []
            # Rewrites pair with the failing drafts in order; one with no usable rewrite keeps its problems.
            rewritten = []
            for d, o in zip(failing, outs, strict=False):
                new = self._draft(o, id=d.id)
                if new is not None:
                    rewritten.append(new)
            again = await self._check(rewritten, seen) if rewritten else {}
            fixed = {d.id: d for d in rewritten if d.id not in again}
            failed = {i: again.get(i, failed[i]) for i in failed if i not in fixed}
            drafts = [fixed.get(d.id, d) for d in drafts]
        kept = [d for d in drafts if d.id not in failed]
        note = None
        if failed:
            first = next(iter(failed.values()))[0]
            note = f"dropped {_plural(len(failed))}: {first.reason}"
        return self._result(kept, note)
