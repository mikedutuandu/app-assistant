"""Wires the nodes (`nodes.py`) into the bot's graph, one run per user message:

    START → read_files → load_context → call_model ⇄ run_tools
                ↓ (cost cap)                ↓ (no tools called: the reply is done)
               END                         END

`call_model` calls the chat model; when it asks for tools, `run_tools` runs them and the graph goes back to
`call_model` with their results, at most `agent.MAX_TOOL_ROUNDS` times before a last call without tools. The cost
cap, a failed model call after tools ran, or a reply without tool calls ends the turn. The thread's state
(`state.py`) is saved by the checkpointer after every node, keyed by `thread_id`.
"""

from collections.abc import Awaitable, Callable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assistant.app_actions import AppActions
from assistant.files.read import FileReading, Meter
from assistant.graph import nodes
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


def _after_model(state: ChatState) -> str:
    return "run_tools" if state.get("tools_pending") else END


def _after_tools(state: ChatState) -> str:
    return END if state.get("capped") or state.get("failed") else "call_model"


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
    actions_factory: Callable[[], AppActions | None] | None = None,
    on_pending: Callable[[dict], Awaitable[str]] | None = None,
) -> CompiledStateGraph:
    """One graph per request (compiling is cheap). Invoke it with `{"messages": [HumanMessage(text)], "file_ids":
    [...], "draft_id": <the draft the message is about, or None>}` and the config `{"configurable": {"thread_id": thread_id(app, workspace, chat_id)}}`.

    `on_cost(cost_usd, n)` is awaited as each model call ends (n counts the turn's calls from 1) and must not raise.
    `actions_factory` reaches the app's MCP server (the pack's `actions`); `on_pending(change)` keeps a change waiting
    for the user's tap and returns its id (`{"tool", "title", "arguments"}`)."""
    deps = Deps(pack, llm, tools_factory, on_event, read_files, read_link, on_cost or _no_cost)
    deps.actions_factory = actions_factory
    deps.on_pending = on_pending
    graph = StateGraph(ChatState)
    for node in (nodes.read_files, nodes.load_context, nodes.call_model, nodes.run_tools):
        graph.add_node(node.__name__, bind(node, deps))
    graph.add_edge(START, "read_files")
    graph.add_conditional_edges("read_files", _after_read, ["load_context", END])
    graph.add_edge("load_context", "call_model")
    graph.add_conditional_edges("call_model", _after_model, ["run_tools", END])
    graph.add_conditional_edges("run_tools", _after_tools, ["call_model", END])
    return graph.compile(checkpointer=checkpointer)
