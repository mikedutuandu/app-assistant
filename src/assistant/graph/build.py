"""Wires the nodes into the bot's graph:

read_files → load_context → converse → END

`converse` is the conversation agent (`agent.py`). A turn reaching the message's cost cap while reading files ends
there; one reaching it inside `converse` ends with what it already did.
"""

from collections.abc import Awaitable, Callable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assistant.files.read import FileReading, Meter
from assistant.graph import agent, nodes
from assistant.graph.state import STATE_TYPES, ChatState
from assistant.graph.turn import Deps, bind
from assistant.llm import Llm
from assistant.packs import AppPack
from assistant.tools import AppTools


def thread_id(app: str, workspace: str, chat_id: str) -> str:
    return f"{app}:{workspace}:{chat_id}"


def serializer() -> JsonPlusSerializer:
    """The checkpointer's serializer, allowing exactly the state's own types (pass it as the saver's `serde`)."""
    return JsonPlusSerializer(allowed_msgpack_modules=[(t.__module__, t.__name__) for t in STATE_TYPES])


def _after_read(state: ChatState) -> str:
    return END if state.get("capped") else "load_context"


async def _no_cost(cost_usd: float, n: int) -> None:
    return None


def build_graph(
    pack: AppPack,
    llm: Llm,
    tools_factory: Callable[[], AppTools],
    checkpointer,
    on_event: Callable[[str, dict], Awaitable[None]],
    read_files: Callable[[list[str], Meter], Awaitable[list[FileReading]]],
    read_link: Callable[[str], Awaitable[FileReading]],
    on_cost: Callable[[float, int], Awaitable[None]] | None = None,
) -> CompiledStateGraph:
    """One graph per request (compiling is cheap). Invoke it with `{"messages": [HumanMessage(text)], "file_ids":
    [...], "draft_id": <the draft the message is about, or None>}` and the config `{"configurable": {"thread_id": thread_id(app, workspace, chat_id)}}`.

    `on_cost(cost_usd, n)` is awaited as each model call ends (n counts the turn's calls from 1) and must not raise."""
    deps = Deps(pack, llm, tools_factory, on_event, read_files, read_link, on_cost or _no_cost)
    graph = StateGraph(ChatState)
    for node in (nodes.read_files, nodes.load_context, agent.converse):
        graph.add_node(node.__name__, bind(node, deps))
    graph.add_edge(START, "read_files")
    graph.add_conditional_edges("read_files", _after_read, ["load_context", END])
    graph.add_edge("load_context", "converse")
    graph.add_edge("converse", END)
    return graph.compile(checkpointer=checkpointer)
