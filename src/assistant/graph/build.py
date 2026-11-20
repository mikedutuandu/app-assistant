"""Wires the nodes into the graph of the spec:

read_files → load_context → interview → (ask_back → END)
                                      | (extract_facts → write → check → (write again once) → present → END)

A turn reaching the message's cost cap ends early, presenting whatever pieces already passed.
"""

from collections.abc import Awaitable, Callable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assistant.files.read import FileReading
from assistant.graph import nodes
from assistant.graph.state import STATE_TYPES, ChatState
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


def _after_interview(state: ChatState) -> str:
    if state.get("capped"):
        return END
    return "extract_facts" if state.get("ready") else "ask_back"


def _after_extract(state: ChatState) -> str:
    if state.get("capped"):
        return END
    return "write" if state.get("facts") else "ask_back"  # never write from nothing


def _after_write(state: ChatState) -> str:
    if state.get("capped"):
        return "present" if state.get("pieces") else END
    return "check"


def _after_check(state: ChatState) -> str:
    if not state.get("capped") and state.get("problems") and not state.get("rewrites"):
        return "write"
    return "present"


def build_graph(
    pack: AppPack,
    llm: Llm,
    tools_factory: Callable[[], AppTools],
    checkpointer,
    on_event: Callable[[str, dict], Awaitable[None]],
    read_files: Callable[[list[str]], Awaitable[list[FileReading]]],
    read_link: Callable[[str], Awaitable[FileReading]],
) -> CompiledStateGraph:
    """One graph per request (compiling is cheap). Invoke it with `{"messages": [HumanMessage(text)], "file_ids":
    [...]}` and the config `{"configurable": {"thread_id": thread_id(app, workspace, chat_id)}}`."""
    deps = nodes.Deps(pack, llm, tools_factory, on_event, read_files, read_link)
    graph = StateGraph(ChatState)
    for node in (
        nodes.read_files,
        nodes.load_context,
        nodes.interview,
        nodes.ask_back,
        nodes.extract_facts,
        nodes.write,
        nodes.check,
        nodes.present,
    ):
        graph.add_node(node.__name__, nodes.bind(node, deps))
    graph.add_edge(START, "read_files")
    graph.add_conditional_edges("read_files", _after_read, ["load_context", END])
    graph.add_edge("load_context", "interview")
    graph.add_conditional_edges("interview", _after_interview, ["extract_facts", "ask_back", END])
    graph.add_edge("ask_back", END)
    graph.add_conditional_edges("extract_facts", _after_extract, ["write", "ask_back", END])
    graph.add_conditional_edges("write", _after_write, ["check", "present", END])
    graph.add_conditional_edges("check", _after_check, ["write", "present"])
    graph.add_edge("present", END)
    return graph.compile(checkpointer=checkpointer)
