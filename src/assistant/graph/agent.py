"""The conversation agent: one chat model that talks with the user and writes posts through tools it chooses to call.

`converse` streams the chat model's reply as `text` and runs the tools it calls: `save_facts` (confirmed facts, in
clean sentences, kept in the thread and saved through the app), `write_drafts` and `revise_draft` (the writer model
and every check, in `assistant.writer`; each passing draft is a `draft` event). Tool results go back to the model as
`role: "tool"` messages, for at most `MAX_TOOL_ROUNDS` calls with tools; then one more call without tools makes it
answer in text. A bad call (no id, no name, an unknown tool, arguments that aren't JSON or don't fit) never raises:
its result tells the model what was wrong. Everything the user, their files, their app or the writer supplied
reaches the model inside `<data>` blocks. The message's cost cap is checked before every chat call and before a
drafting tool runs; reaching it ends the turn with the cap text, keeping what the turn already did. A model call
that fails after tools ran ends the turn as `failed` (state), likewise keeping what they did.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field, StrictInt, StringConstraints, ValidationError

from assistant.checks import Fact, nfc, numbers_in, unsupported_numbers
from assistant.db import new_id
from assistant.graph import prompts
from assistant.graph.prompts import data
from assistant.graph.state import ChatState
from assistant.graph.turn import CAP_TEXT, Capped, Turn
from assistant.llm import LlmError, ToolCall, ToolTurn
from assistant.tools import AppContext, ToolError
from assistant.writer import Draft, DraftResult, WriterDeps, fmt, language_tag, revise_draft, write_drafts

MAX_TOOL_ROUNDS = 4  # chat calls with tools in one message; then one more without
CHAT_TOKENS = 1200
MAX_MESSAGES = 20  # conversation messages shown to the model
MAX_FACTS = 10  # facts in one save_facts call
FACT_CHARS = 300
MAX_RECENT = 5  # the user's recent posts shown to the chat model (the writer sees them all)
MAX_DRAFTS_SHOWN = 10  # this thread's latest drafts in the system prompt

TOOLS = ("write_drafts", "revise_draft", "save_facts")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_drafts",
            "description": (
                "Write drafts for X from the facts saved in this chat, in the user's voice. Every draft is checked "
                "before the user sees it; the user sees each one under your reply. Call it only when there is a real "
                "story, or when the user asks you to write and there are facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brief": {"type": "string", "description": "What the post is about and its angle, in a sentence."},
                    "kind": {
                        "type": "string",
                        "enum": ["x_post", "x_thread"],
                        "description": "x_post for one post; x_thread only when the user asks for a thread.",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "How many drafts: 1 unless the user asks for more.",
                    },
                },
                "required": ["brief", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise_draft",
            "description": (
                "Write a new version of one of this chat's drafts as the user asks (shorter, another angle, add a "
                "detail). The new version is checked and shown under your reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "The id of the draft to change."},
                    "instruction": {"type": "string", "description": "What to change, in a few words."},
                },
                "required": ["draft_id", "instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_facts",
            "description": (
                "Save facts the user confirmed or stated clearly, each as one short, clean sentence in their "
                "language with their numbers exactly as given. Drafts are written only from saved facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_FACTS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "maxLength": FACT_CHARS},
                                "file_id": {
                                    "type": "string",
                                    "description": "The file_id of the file or link it was read from, if any.",
                                },
                            },
                            "required": ["text"],
                        },
                    }
                },
                "required": ["facts"],
            },
        },
    },
]


# ---- the tools' arguments ----

_Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class _WriteArgs(BaseModel):
    brief: Annotated[_Text, StringConstraints(max_length=2000)]
    kind: Literal["x_post", "x_thread"]
    count: StrictInt = Field(1, ge=1, le=3)


class _ReviseArgs(BaseModel):
    draft_id: _Text
    instruction: Annotated[_Text, StringConstraints(max_length=500)]


class _FactArg(BaseModel):
    text: Annotated[_Text, StringConstraints(max_length=FACT_CHARS)]
    file_id: Annotated[str, StringConstraints(strict=True)] | None = None


class _SaveArgs(BaseModel):
    facts: list[_FactArg] = Field(min_length=1, max_length=MAX_FACTS)


def _invalid(e: ValidationError) -> str:
    found = [f"{'.'.join(str(x) for x in err['loc']) or 'arguments'}: {err['msg']}" for err in e.errors()[:3]]
    return f"error: invalid arguments ({'; '.join(found)}); nothing was done"


# ---- helpers ----


def _content(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return " ".join(p.get("text", "") for p in message.content if isinstance(p, dict))


def _key(text: str) -> str:
    """How facts are matched: case, spacing and end punctuation aside."""
    return " ".join(nfc(text).casefold().split()).strip(" .!?;:,")


def _draft_block(d: Draft) -> str:
    if len(d.posts) == 1:
        text = d.posts[0]
    else:
        text = "\n\n".join(f"({i}/{len(d.posts)}) {p}" for i, p in enumerate(d.posts, 1))
    return data("draft", text, id=d.id, kind=d.kind)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


@dataclass
class _Call:
    """A tool call as sent back to the model: always an id and a well-formed name, with the arguments as JSON."""

    id: str
    name: str
    arguments: str
    problem: str | None  # set when the call is not run


@dataclass
class _Agent:
    state: ChatState
    t: Turn
    facts: list[Fact] = field(default_factory=list)
    drafts: list[Draft] = field(default_factory=list)
    family: dict[str, str] = field(default_factory=dict)
    parts: list[str] = field(default_factory=list)  # what the model said, round by round
    said: str = ""  # this round's text so far
    results: list[tuple[str, str]] = field(default_factory=list)  # (tool, result), this turn
    ids: set[str] = field(default_factory=set)  # the tool call ids sent back this turn

    def __post_init__(self) -> None:
        self.facts = list(self.state.get("facts", []))
        self.drafts = list(self.state.get("drafts", []))
        self.family = dict(self.state.get("draft_family", {}))

    @property
    def context(self) -> AppContext | None:
        return self.state.get("context")

    # -- what the model is shown --

    def _system(self) -> str:
        ctx = self.context
        parts = [
            prompts.CONVERSE,
            f"App: {self.t.pack.name}",
            f"Language: {language_tag(ctx.language if ctx else '')}",
            f"The app's guide:\n{self.t.pack.guide.strip()}",
        ]
        name = self.state.get("user_name")
        if name:
            parts.append("The user's name: " + data("context", name))
        if ctx is not None:
            recent = "\n".join(f"- {p}" for p in ctx.recent_posts[:MAX_RECENT]) or "none"
            about = (
                f"Voice: {ctx.voice}\nTopics: {', '.join(ctx.topics)}\nPatterns that work: {'; '.join(ctx.patterns)}\n"
                f"Their latest posts:\n{recent}"
            )
            parts.append("About the user, from their app:\n" + data("context", about))
            if ctx.kept_facts:
                kept = "\n".join(data("kept", k.text, id=k.id) for k in ctx.kept_facts)
                parts.append("Facts they kept in an earlier chat, not used in a post yet:\n" + kept)
        facts = "\n".join(data("fact", f.text) for f in self.facts) or "none yet"
        parts.append("Facts saved in this chat:\n" + facts)
        readings = self.state.get("readings", [])
        if readings:
            blocks = []
            for r in readings:
                text = r.text + (f"\nUnsure (not facts): {'; '.join(r.unsure)}" if r.unsure else "")
                blocks.append(data(r.kind, text, file_id=r.file_id))
            parts.append("Files and links they shared:\n" + "\n".join(blocks))
        shown = "\n".join(_draft_block(d) for d in self.drafts[-MAX_DRAFTS_SHOWN:]) or "none yet"
        parts.append("Drafts in this chat, oldest first (the user sees each one with its buttons):\n" + shown)
        return "\n\n".join(parts)

    def _history(self) -> list[dict]:
        messages = [m for m in self.state.get("messages", []) if isinstance(m, HumanMessage | AIMessage)]
        messages = messages[-MAX_MESSAGES:]
        while messages and not isinstance(messages[0], HumanMessage):
            messages = messages[1:]  # the conversation shown starts with the user
        last = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        out: list[dict] = []
        for i, m in enumerate(messages):
            text = nfc(_content(m)).strip()
            if isinstance(m, AIMessage):
                if text:
                    out.append({"role": "assistant", "content": text})
                continue
            content = data("user", text) if text else "(No message: the user sent a file.)"
            about = self._about() if i == last else None
            if about is not None:
                content = f"(This message is about draft {about.id}.)\n{content}"
            out.append({"role": "user", "content": content})
        return out

    def _about(self) -> Draft | None:
        wanted = self.state.get("draft_id")
        return next((d for d in self.drafts if d.id == wanted), None) if wanted else None

    # -- the loop --

    async def _forward(self, delta: str) -> None:
        if not delta:
            return
        if not self.said and self.parts:  # an earlier round said something: a new paragraph
            await self.t.emit("text", {"delta": "\n\n"})
        self.said += delta
        await self.t.emit("text", {"delta": delta})

    def _end_round(self) -> None:
        if self.said.strip():
            self.parts.append(self.said.strip())
        self.said = ""

    async def run(self) -> None:
        messages = [{"role": "system", "content": ""}, *self._history()]
        start = len(messages)
        for n in range(MAX_TOOL_ROUNDS + 1):
            with_tools = n < MAX_TOOL_ROUNDS
            messages[0] = {"role": "system", "content": self._system()}  # facts and drafts as they are now
            sent = messages if with_tools else self._final(messages[:start])
            turn: ToolTurn = await self.t.chat_tools(sent, TOOL_SPECS if with_tools else [], CHAT_TOKENS, self._forward)
            if not self.said and turn.text.strip():
                await self._forward(turn.text)  # text the stream never handed over
            text = self.said.strip()
            self._end_round()
            if not with_tools or not turn.calls:
                return  # calls in the answer without tools are never run
            calls = self._prepare(turn.calls, n + 1)
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in calls
                    ],
                }
            )
            for c in calls:
                result = c.problem or await self._run(c)
                self.results.append((c.name, result))
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})

    def _final(self, base: list[dict]) -> list[dict]:
        """The last call has no tools, so it carries no tool messages (an API refuses those without tools): what the
        model said and what its tools answered this turn come as plain messages."""
        out = list(base)
        if self.parts:
            out.append({"role": "assistant", "content": "\n\n".join(self.parts)})
        done = "\n".join(f"- {name}: {result}" for name, result in self.results)
        out.append(
            {
                "role": "user",
                "content": "(Tools are no longer available for this message. What your tools answered:\n"
                f"{done}\nNow answer the user in text.)",
            }
        )
        return out

    def _prepare(self, calls: list[ToolCall], n: int) -> list[_Call]:
        """Each call with an id unique in this turn (one the model gave twice gets a new one; the results must pair
        with their calls) and a name an API takes back; a call that can't be run carries why."""
        out: list[_Call] = []
        for i, c in enumerate(calls, 1):
            given = (c.id or "").strip()
            name = c.name if _NAME.fullmatch(c.name or "") else "unnamed"
            arguments = json.dumps(c.arguments) if c.bad_arguments is None else "{}"
            problem = None
            if not given:
                problem = "error: the call had no id, so it wasn't run; call it again"
            elif not c.name:
                problem = f"error: the call had no name; the tools are {', '.join(TOOLS)}"
            elif c.name not in TOOLS:
                problem = f"error: unknown tool {name!r}; the tools are {', '.join(TOOLS)}"
            elif c.bad_arguments is not None:
                problem = "error: the arguments weren't valid JSON; nothing was done"
            id = given if given and given not in self.ids else f"bot_{n}_{i}"
            while id in self.ids:
                id += "_"
            self.ids.add(id)
            out.append(_Call(id=id, name=name, arguments=arguments, problem=problem))
        return out

    async def _run(self, c: _Call) -> str:
        arguments = json.loads(c.arguments)
        try:
            if c.name == "save_facts":
                return await self._save_facts(_SaveArgs.model_validate(arguments))
            if c.name == "write_drafts":
                return await self._write(_WriteArgs.model_validate(arguments))
            return await self._revise(_ReviseArgs.model_validate(arguments))
        except ValidationError as e:
            return _invalid(e)

    # -- the tools --

    def _said_texts(self) -> list[str]:
        """What the user said and showed in this thread, and the facts they kept earlier (their own words)."""
        said = [nfc(_content(m)) for m in self.state.get("messages", []) if isinstance(m, HumanMessage)]
        kept = [k.text for k in self.context.kept_facts] if self.context else []
        return said + [r.text for r in self.state.get("readings", [])] + kept

    async def _save_facts(self, args: _SaveArgs) -> str:
        readings = {r.file_id: r for r in self.state.get("readings", [])}
        kept = {_key(k.text): k for k in (self.context.kept_facts if self.context else [])}
        said = [Fact(numbers=numbers_in(t)) for t in self._said_texts()]
        keys = {_key(f.text) for f in self.facts}
        new: list[Fact] = []
        refused: list[str] = []
        for item in args.facts:
            text = nfc(item.text).strip()
            reading = readings.get(item.file_id) if item.file_id is not None else None
            if item.file_id is not None and reading is None:
                refused.append(f"{text!r}: its file_id is not a file shared in this chat")
                continue
            was_kept = kept.get(_key(text))
            if was_kept is not None:
                text = was_kept.text  # word for word, with its own id: the app marks it used by that
            if _key(text) in keys:
                continue
            missing = unsupported_numbers(text, said)
            if missing:
                refused.append(f"{text!r}: it has a number the user never gave ({fmt(missing[0])})")
                continue
            keys.add(_key(text))
            new.append(
                Fact(
                    id=was_kept.id if was_kept is not None else new_id(),
                    text=text,
                    numbers=numbers_in(text),
                    source=reading.kind if reading is not None and was_kept is None else "typed",  # type: ignore[arg-type]
                    file_id=reading.file_id if reading is not None and was_kept is None else None,
                )
            )
        if new:
            self.facts.extend(new)
            try:
                await self.t.deps.tools_factory().save_material(new, None, f"facts:{new_id()}")
            except ToolError:
                result = f"kept {_plural(len(new), 'fact')} for this chat, but the app couldn't save them for later"
            else:
                await self.t.emit("saved", {"kind": "facts", "items": [f.text for f in new]})
                result = f"saved {len(new)} facts"
        else:
            result = "nothing new to save" if not refused else "saved 0 facts"
        if refused:
            result += "; not saved: " + "; ".join(refused)
        return result

    def _writer(self, exclude: set[str]) -> WriterDeps:
        ctx = self.context
        recent = [*(ctx.recent_posts if ctx else []), *(p for d in self.drafts if d.id not in exclude for p in d.posts)]

        async def status(step: str) -> None:
            await self.t.emit("status", {"step": step})

        return WriterDeps(
            pack=self.t.pack,
            llm=self.t.deps.llm,
            facts=list(self.facts),
            readings=list(self.state.get("readings", [])),
            context=ctx,
            recent=recent,
            spend=self.t.spend,
            status=status,
        )

    async def _write(self, args: _WriteArgs) -> str:
        self.t.gate()
        result = await write_drafts(self._writer(set()), args.brief, args.kind, args.count)
        return await self._shown(result, root=None)

    async def _revise(self, args: _ReviseArgs) -> str:
        draft = next((d for d in self.drafts if d.id == args.draft_id), None)
        if draft is None:
            ids = ", ".join(d.id for d in self.drafts[-MAX_DRAFTS_SHOWN:]) or "none"
            return f"error: there is no draft {args.draft_id[:64]!r} in this chat; its drafts: {ids}"
        self.t.gate()
        # Earlier versions of the same draft aren't repeats of it: "shorter" twice must not drop the second version.
        root = self.family.get(draft.id, draft.id)
        line = {d.id for d in self.drafts if self.family.get(d.id, d.id) == root}
        result = await revise_draft(self._writer(line), draft, args.instruction)
        return await self._shown(result, root=root)

    async def _shown(self, result: DraftResult, root: str | None) -> str:
        for d in result.drafts:
            self.drafts.append(d)
            if root is not None:
                self.family[d.id] = root
            payload: dict = {"id": d.id, "kind": d.kind, "posts": d.posts}
            if d.image_file_id is not None:
                payload["image_file_id"] = d.image_file_id
            await self.t.emit("draft", payload)
        if not result.drafts:
            return f"nothing written: {result.note or 'the writer returned nothing usable'}"
        n = len(result.drafts)
        out = (
            f"wrote {_plural(n, 'draft')}; the user already sees {'it' if n == 1 else 'them'} under your reply, "
            "so don't repeat the text:\n" + "\n".join(_draft_block(d) for d in result.drafts)
        )
        return out + (f"\nnote: {result.note}" if result.note else "")

    # -- the end --

    async def capped(self) -> None:
        self._end_round()
        if self.parts:
            await self.t.emit("text", {"delta": "\n\n"})
        await self.t.emit("text", {"delta": CAP_TEXT})

    def update(self, capped: bool = False, failed: bool = False) -> dict:
        out: dict = {"facts": self.facts, "drafts": self.drafts, "draft_family": self.family, "draft_id": None}
        if self.parts:
            out["messages"] = [AIMessage(content="\n\n".join(self.parts))]
        if capped:
            out["capped"] = True
        if failed:
            out["failed"] = True
        return out


async def converse(state: ChatState, t: Turn) -> dict:
    """Replies to the user's message, running the tools the chat model calls (see the module's docstring)."""
    await t.emit("status", {"step": "thinking"})
    agent = _Agent(state, t)
    try:
        await agent.run()
    except Capped:
        await agent.capped()
        return agent.update(capped=True)
    except LlmError:
        if not agent.results:
            raise  # nothing happened yet: the turn fails as any failed call does (bind carries its cost)
        # Tools already ran (drafts shown, facts saved in the app): keep their work in the thread, so a later
        # "shorter" finds the draft and the facts aren't saved again, and let the turn end as failed.
        agent.said = ""  # what the failed call had started to say is not kept
        return agent.update(failed=True)
    return agent.update()
