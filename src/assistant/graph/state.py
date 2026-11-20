"""The conversation state LangGraph keeps per thread (`{app}:{workspace}:{chat_id}`)."""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

from assistant.checks import Fact
from assistant.files.read import FileReading
from assistant.tools import AppContext, KeptFact
from assistant.writer import Draft


class ChatState(TypedDict, total=False):
    # Kept for the whole thread
    messages: Annotated[list, add_messages]  # the user's messages and the bot's replies (text only)
    facts: list[Fact]  # confirmed facts, in clean sentences (save_facts); accumulate across turns
    readings: list[FileReading]  # every file and link read in this thread
    drafts: list[Draft]  # every draft shown in this thread, oldest first (revisions are new drafts)
    draft_family: dict[str, str]  # a revised draft's id -> the id of the draft its line started from
    user_name: str | None  # from the app's context, when it knows it
    context: AppContext | None
    context_at: float  # when `context` was loaded (epoch seconds)

    # This turn's input
    file_ids: list[str]
    draft_id: str | None  # the draft this message is about (its Shorter or Another angle), if any

    # This turn's work (reset by read_files)
    cost_usd: float
    capped: bool  # the message's cost cap stopped the turn
    failed: bool  # a model call failed after tools had run: the turn fails, keeping what they did


# Types stored in checkpoints, for the checkpointer's msgpack allowlist.
STATE_TYPES = [Draft, Fact, FileReading, AppContext, KeptFact]
