"""The conversation agent's work in one turn, shared by the graph's `call_model` and `run_tools` nodes.

`call_model` makes one call of the chat model (streamed as `text`), offering the three tools (`agent_tools.py`).
`run_tools` runs the calls it made: `save_facts` (confirmed facts, kept in the thread and saved through the app),
`write_drafts` and `revise_draft` (the writer model and every check, in `assistant.writer`; each passing draft is a
`draft` event). Their results go back to the model as `role: "tool"` messages and the graph returns to `call_model`,
for at most `MAX_TOOL_ROUNDS` calls with tools; then one more call without tools makes it answer in text.

`AgentTurn` holds what the rounds of one turn share and the thread doesn't keep: the messages with their tool calls
and results, and the reply as shown so far (`reply_text.py`). The thread keeps only what `update()` returns: facts,
drafts and the reply's text.
"""

import json
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from assistant.checks import Fact, nfc, numbers_in, unsupported_numbers
from assistant.db import new_id
from assistant.graph.agent_tools import (
    TOOL_SPECS,
    PreparedCall,
    ReviseDraftArgs,
    SaveFactsArgs,
    WriteDraftsArgs,
    invalid_arguments,
    prepare_calls,
)
from assistant.graph.model_input import (
    MAX_DRAFTS_SHOWN,
    draft_block,
    final_messages,
    history,
    message_text,
    system_prompt,
)
from assistant.graph.reply_text import ReplyText
from assistant.graph.state import ChatState
from assistant.graph.turn import Turn
from assistant.tools import AppContext, ToolError
from assistant.writer import Draft, DraftResult, WriterDeps, fmt, revise_draft, write_drafts

MAX_TOOL_ROUNDS = 4  # chat calls with tools in one message; then one more without
CHAT_TOKENS = 1200


def _key(text: str) -> str:
    """How facts are matched: case, spacing and end punctuation aside."""
    return " ".join(nfc(text).casefold().split()).strip(" .!?;:,")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


@dataclass
class AgentTurn:
    state: ChatState  # the thread as the turn found it
    reply: ReplyText
    facts: list[Fact] = field(default_factory=list)
    drafts: list[Draft] = field(default_factory=list)
    family: dict[str, str] = field(default_factory=dict)  # a revised draft's id -> the first draft of its line
    written: set[str] = field(default_factory=set)  # the ids of the drafts written in this message
    messages: list[dict] = field(default_factory=list)  # this turn's chat messages, with tool calls and results
    start: int = 0  # where this turn's tool messages begin in `messages`
    rounds: int = 0  # chat calls made this turn
    pending: list[PreparedCall] = field(default_factory=list)  # the last call's tool calls, waiting for run_tools
    results: list[tuple[str, str]] = field(default_factory=list)  # (tool, result), this turn
    ids: set[str] = field(default_factory=set)  # the tool call ids sent back this turn

    @classmethod
    def begin(cls, state: ChatState, turn: Turn) -> "AgentTurn":
        agent = cls(state=state, reply=ReplyText(turn.emit))
        agent.facts = list(state.get("facts", []))
        agent.drafts = list(state.get("drafts", []))
        agent.family = dict(state.get("draft_family", {}))
        agent.messages = [{"role": "system", "content": ""}, *history(state, agent.about())]
        agent.start = len(agent.messages)
        return agent

    @property
    def context(self) -> AppContext | None:
        return self.state.get("context")

    def about(self) -> Draft | None:
        """The draft this message is about, when it came from one of the draft's buttons."""
        wanted = self.state.get("draft_id")
        return next((d for d in self.drafts if d.id == wanted), None) if wanted else None

    # ---- one round: call the model, then (in run_tools) its tools ----

    async def call_model(self, turn: Turn) -> bool:
        """One chat call. True when it called tools, now in `pending` for `run_tools`."""
        with_tools = self.rounds < MAX_TOOL_ROUNDS
        self.messages[0] = {
            "role": "system",
            "content": system_prompt(turn.pack, self.state, self.facts, self.drafts, self.written),
        }  # facts and drafts as they are now
        sent = (
            self.messages if with_tools else final_messages(self.messages[: self.start], self.reply.parts, self.results)
        )
        out = await turn.chat_tools(sent, TOOL_SPECS if with_tools else [], CHAT_TOKENS, self.reply.forward)
        self.rounds += 1
        if not self.reply.said and out.text.strip():
            await self.reply.forward(out.text)  # text the stream never handed over
        await self.reply.release()
        text = self.reply.said.strip()
        self.reply.end_round()
        if not with_tools or not out.calls:
            return False  # calls in the answer without tools are never run
        self.pending = prepare_calls(out.calls, self.rounds, self.ids)
        self.messages.append(
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in self.pending
                ],
            }
        )
        return True

    async def run_tools(self, turn: Turn) -> None:
        """Runs the pending calls in order; each result goes back to the model as a tool message."""
        calls, self.pending = self.pending, []
        for c in calls:
            result = c.problem or await self._run(c, turn)
            self.results.append((c.name, result))
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": result})

    async def _run(self, c: PreparedCall, turn: Turn) -> str:
        arguments = json.loads(c.arguments)
        try:
            if c.name == "save_facts":
                return await self._save_facts(SaveFactsArgs.model_validate(arguments), turn)
            if c.name == "write_drafts":
                return await self._write(WriteDraftsArgs.model_validate(arguments), turn)
            return await self._revise(ReviseDraftArgs.model_validate(arguments), turn)
        except ValidationError as e:
            return invalid_arguments(e)

    # ---- the tools ----

    def _said_texts(self) -> list[str]:
        """What the user said and showed in this thread, and the facts they kept earlier (their own words)."""
        said = [nfc(message_text(m)) for m in self.state.get("messages", []) if isinstance(m, HumanMessage)]
        kept = [k.text for k in self.context.kept_facts] if self.context else []
        return said + [r.text for r in self.state.get("readings", [])] + kept

    async def _save_facts(self, args: SaveFactsArgs, turn: Turn) -> str:
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
                await turn.deps.tools_factory().save_material(new, None, f"facts:{new_id()}")
            except ToolError:
                result = f"kept {_plural(len(new), 'fact')} for this chat, but the app couldn't save them for later"
            else:
                await self.reply.emit("saved", {"kind": "facts", "items": [f.text for f in new]})
                result = f"saved {len(new)} facts"
        else:
            result = "nothing new to save" if not refused else "saved 0 facts"
        if refused:
            result += "; not saved: " + "; ".join(refused)
        return result

    def _writer(self, turn: Turn, kind: str, exclude: set[str]) -> WriterDeps:
        """The writer's inputs. A draft must not repeat the user's published posts, nor this chat's drafts of the same
        kind: a thread retelling a post's news may reuse its sentences."""
        ctx = self.context
        mine = [p for d in self.drafts if d.kind == kind and d.id not in exclude for p in d.posts]

        async def status(step: str) -> None:
            await self.reply.emit("status", {"step": step})

        return WriterDeps(
            pack=turn.pack,
            llm=turn.llm,
            facts=list(self.facts),
            readings=list(self.state.get("readings", [])),
            context=ctx,
            recent=list(ctx.recent_posts if ctx else []),
            drafts=mine,
            spend=turn.spend,
            status=status,
        )

    async def _write(self, args: WriteDraftsArgs, turn: Turn) -> str:
        turn.gate()
        writer = self._writer(turn, args.kind, set())
        result = await write_drafts(writer, args.brief, args.kind, args.count, args.language)
        return await self._shown(result, root=None)

    async def _revise(self, args: ReviseDraftArgs, turn: Turn) -> str:
        draft = next((d for d in self.drafts if d.id == args.draft_id), None)
        if draft is None:
            ids = ", ".join(d.id for d in self.drafts[-MAX_DRAFTS_SHOWN:]) or "none"
            return f"error: there is no draft {args.draft_id[:64]!r} in this chat; its drafts: {ids}"
        turn.gate()
        # Earlier versions of the same draft aren't repeats of it: "shorter" twice must not drop the second version.
        root = self.family.get(draft.id, draft.id)
        line = {d.id for d in self.drafts if self.family.get(d.id, d.id) == root}
        # A tap on the draft's own buttons sends the app's (English) words: the new version keeps the draft's language.
        tapped = self.about() is not None and self.about().id == draft.id
        language = draft.language if tapped and draft.language else (args.language or draft.language)
        result = await revise_draft(self._writer(turn, draft.kind, line), draft, args.instruction, language)
        return await self._shown(result, root=root)

    async def _shown(self, result: DraftResult, root: str | None) -> str:
        """Shows each new draft (a `draft` event) and tells the model what was written."""
        first = not self.drafts  # the chat had no draft before this call (review N3)
        for d in result.drafts:
            self.drafts.append(d)
            self.written.add(d.id)
            if root is not None:
                self.family[d.id] = root
            payload: dict = {"id": d.id, "kind": d.kind, "posts": d.posts}
            if d.image_file_id is not None:
                payload["image_file_id"] = d.image_file_id
            # `told`: the facts the draft was written from, for the API to store with it. Never sent to the browser.
            payload["told"] = [f.model_dump() for f in self.facts if f.id in set(d.fact_ids)]
            await self.reply.emit("draft", payload)
        if not result.drafts:
            return f"nothing written: {result.note or 'the writer returned nothing usable'}"
        n = len(result.drafts)
        language = result.drafts[0].language or "the conversation's language"
        out = (
            f"wrote {_plural(n, 'draft')} in {language}; the user already sees {'it' if n == 1 else 'them'} under "
            f"your reply. Answer in one short sentence in {language}; don't repeat or describe the "
            f"draft{'' if n == 1 else 's'}."
        )
        if first:
            out += (
                " This is the chat's first draft; never call it a fresh take, another version or a new one."
                if n == 1
                else " These are the chat's first drafts; never call them fresh takes, other versions or new ones."
            )
        out += "\n" + "\n".join(draft_block(d) for d in result.drafts)
        return out + (f"\nnote: {result.note}" if result.note else "")

    # ---- what the thread keeps ----

    def update(self, done: bool, capped: bool = False, failed: bool = False) -> dict:
        """The state update after a node. While the turn goes on, the facts and drafts so far; once it is `done`,
        the reply's text too, and the turn's input is cleared."""
        out: dict = {
            "facts": self.facts,
            "drafts": self.drafts,
            "draft_family": self.family,
            "tools_pending": bool(self.pending) and not done,
        }
        if done:
            out["draft_id"] = None
            out["ask"] = []
            if self.reply.parts:
                out["messages"] = [AIMessage(content=self.reply.reply)]
        if capped:
            out["capped"] = True
        if failed:
            out["failed"] = True
        return out
