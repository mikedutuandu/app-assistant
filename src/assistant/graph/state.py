"""The conversation state LangGraph keeps per thread (`{app}:{workspace}:{chat_id}`)."""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages
from pydantic import BaseModel

from assistant.checks import Fact
from assistant.files.read import FileReading
from assistant.tools import AppContext


class Piece(BaseModel):
    id: str
    spec_kind: str
    text: str
    image_file_id: str | None = None
    fact_ids: list[str] = []
    dropped: str | None = None  # the reason shown when it failed twice


class ChatState(TypedDict, total=False):
    # Kept for the whole thread
    messages: Annotated[list, add_messages]
    facts: list[Fact]  # accumulate across turns
    readings: list[FileReading]  # every file and link read in this thread
    rounds_asked: int  # ask-back rounds since the last card
    context: AppContext | None
    context_at: float  # when `context` was loaded (epoch seconds)
    past_pieces: list[str]  # texts of pieces shown in earlier turns, for the repeat check

    # This turn's input
    file_ids: list[str]

    # This turn's work (reset by read_files)
    cost_usd: float
    pieces: list[Piece]
    just_write: bool
    ready: bool  # the interview found enough to write
    ask_hint: str | None  # what the interview would ask about
    problems: dict[str, list[str]]  # piece id -> reasons it failed the check
    passed: list[str]  # ids of pieces that passed the check
    rewrites: int  # 1 once failing pieces were written again
    capped: bool  # the message's cost cap stopped the turn


# Types stored in checkpoints, for the checkpointer's msgpack allowlist.
STATE_TYPES = [Piece, Fact, FileReading, AppContext]
