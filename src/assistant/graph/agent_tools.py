"""The three tools the chat model may call: what it is told about them, and how a call is checked before it runs.

- `tool_specs(pack)` is what the model reads (JSON schema in OpenRouter's tool format). It is written for the model,
  and the recorded conversations pin it byte for byte (`scripts/converse_eval.py`). `write_drafts` offers the kinds
  of writing the app's pack declares (`WriteSpec`), by name: nothing here names a platform.
- `WriteDraftsArgs`, `ReviseDraftArgs` and `SaveFactsArgs` check a call's arguments before the tool runs.
  `tests/test_agent_tools.py` keeps the two in step: the same tools, fields and required fields.
- `prepare_calls` turns the model's calls into calls that can be answered: each with an id unique in the turn and a
  well-formed name, and a call that can't be run carries the reason instead (it never raises).

What each tool does is in `agent.py` (`AgentTurn.run_call`).
"""

import copy
import json
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, StrictInt, StringConstraints, ValidationError

from assistant.llm import ToolCall
from assistant.packs import AppPack
from assistant.writer import clean_language

MAX_FACTS = 10  # facts in one save_facts call
FACT_CHARS = 300

TOOLS = ("write_drafts", "revise_draft", "save_facts")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

_TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_drafts",
            "description": (
                "Write drafts from the facts saved in this chat, in the user's voice. Every draft is checked "
                "before the user sees it; the user sees each one under your reply. Call it when there is a real "
                "story, or when the user asks you to write: save what they told you first (save_facts, in the same "
                "message)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "brief": {"type": "string", "description": "What the draft is about and its angle, in a sentence."},
                    "kind": {"type": "string"},  # the pack's kinds: tool_specs fills in the enum and description
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "How many drafts: 1 unless the user asks for more.",
                    },
                    "language": {
                        "type": "string",
                        "description": (
                            "The language to write in, as a short tag (en, vi, pt-BR): the one the user talks with you "
                            "in, never the language of a button's text."
                        ),
                    },
                    "image_file_id": {
                        "type": "string",
                        "description": (
                            "The file_id of an image the user shared, to post with the drafts: only when they asked "
                            "for it or said yes when you asked."
                        ),
                    },
                    "post_at": {
                        "type": "string",
                        "description": "When to post it: the `at` of one of the app's free times, the one that fits best.",
                    },
                },
                "required": ["brief", "kind", "language"],
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
                    "language": {
                        "type": "string",
                        "description": (
                            "The language to write in, as a short tag (en, vi, pt-BR): the one the user talks with you "
                            "in, never the language of a button's text."
                        ),
                    },
                    "image_file_id": {
                        "type": "string",
                        "description": (
                            "The file_id of an image the user shared, to post with it, when they ask to add one. "
                            "Left out, the draft keeps its own picture."
                        ),
                    },
                },
                "required": ["draft_id", "instruction", "language"],
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


def _kinds(pack: AppPack) -> dict:
    """`write_drafts`'s `kind`: the kinds the app writes, each with its name and when to write it."""
    lines = [f"{w.kind}: {w.name}" + (f", {w.about}" if w.about else "") for w in pack.writes]
    return {"type": "string", "enum": [w.kind for w in pack.writes], "description": "; ".join(lines) + "."}


def tool_specs(pack: AppPack) -> list[dict]:
    """The three tools as the model is told about them, for this app's kinds of writing."""
    specs = copy.deepcopy(_TOOL_SPECS)
    specs[0]["function"]["parameters"]["properties"]["kind"] = _kinds(pack)
    return specs


# ---- each tool's arguments, checked before it runs ----

_Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


# The draft's language: a tag the writer takes, else None and the writer uses the app's (never an error).
_Language = Annotated[str | None, BeforeValidator(lambda v: clean_language(v) if isinstance(v, str) else None)]


# An optional id or time: anything but a non-empty string is left out (checked against the chat when the tool runs).
_Optional = Annotated[str | None, BeforeValidator(lambda v: v.strip()[:100] or None if isinstance(v, str) else None)]


class WriteDraftsArgs(BaseModel):
    brief: Annotated[_Text, StringConstraints(max_length=2000)]
    kind: _Text  # one of the pack's kinds (the writer answers an unknown one with the kinds it can write)
    count: StrictInt = Field(1, ge=1, le=3)
    language: _Language = None
    image_file_id: _Optional = None
    post_at: _Optional = None


class ReviseDraftArgs(BaseModel):
    draft_id: _Text
    instruction: Annotated[_Text, StringConstraints(max_length=500)]
    language: _Language = None
    image_file_id: _Optional = None


class FactArg(BaseModel):
    text: Annotated[_Text, StringConstraints(max_length=FACT_CHARS)]
    file_id: Annotated[str, StringConstraints(strict=True)] | None = None


class SaveFactsArgs(BaseModel):
    facts: list[FactArg] = Field(min_length=1, max_length=MAX_FACTS)


def invalid_arguments(e: ValidationError) -> str:
    found = [f"{'.'.join(str(x) for x in err['loc']) or 'arguments'}: {err['msg']}" for err in e.errors()[:3]]
    return f"error: invalid arguments ({'; '.join(found)}); nothing was done"


# ---- a model's calls, made answerable ----


@dataclass
class PreparedCall:
    """A tool call as sent back to the model: always an id and a well-formed name, with the arguments as JSON."""

    id: str
    name: str
    arguments: str
    problem: str | None  # set when the call is not run: the reason, which becomes its result


def prepare_calls(calls: list[ToolCall], round_no: int, used_ids: set[str]) -> list[PreparedCall]:
    """Each call with an id unique in this turn (one the model gave twice gets a new one; the results must pair
    with their calls) and a name an API takes back; a call that can't be run carries why. Adds the ids to `used_ids`."""
    out: list[PreparedCall] = []
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
        id = given if given and given not in used_ids else f"bot_{round_no}_{i}"
        while id in used_ids:
            id += "_"
        used_ids.add(id)
        out.append(PreparedCall(id=id, name=name, arguments=arguments, problem=problem))
    return out
