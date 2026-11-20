"""Runs the conversation graph with test doubles: a scripted FakeLlm, FakeTools, files and links from dicts."""

from fakes import FakeLlm, FakeTools
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from assistant.files.read import FileReading, Meter
from assistant.graph.build import build_graph, serializer, thread_id
from assistant.packs import AppPack, Models, WriteSpec

CAP_TEXT = "That's as far as I can go on this message. Send another to continue."
RULES = {"max_chars": 280, "no_links": True, "no_dashes": True, "may_carry_image": True}


def make_pack(cap: float = 0.05) -> AppPack:
    return AppPack(
        id="demo",
        name="Shipnote",
        mcp_url="http://testserver/demo/mcp",
        secret_env="DEMO_SESSION_SECRET",
        allowed_origins=["http://testserver"],
        guide="Ask what shipped and for one number.",
        writes=[
            WriteSpec(kind="x_post", instructions="A short post for X.", **RULES),
            WriteSpec(kind="x_thread", max_posts=4, instructions="A short thread for X.", **RULES),
        ],
        tools={"context": "c", "save_draft": "s", "save_material": "m", "charge": "ch"},
        models=Models(
            chat="chat/model",
            writer="writer/model",
            main="main/model",
            cheap="cheap/model",
            vision="vis/model",
            fallbacks=["fallback/model"],
        ),
        message_usd_cap=cap,
        demo=True,
    )


class Harness:
    def __init__(self, llm: FakeLlm, tools: FakeTools | None = None, pack: AppPack | None = None):
        self.llm = llm
        self.tools = tools or FakeTools()
        self.pack = pack or make_pack()
        self.saver = InMemorySaver(serde=serializer())
        self.events: list[tuple[str, dict]] = []
        self.files: dict[str, FileReading | Exception] = {}
        self.file_costs: dict[str, float] = {}  # what reading a refused file cost (a too-long voice note)
        self.links: dict[str, FileReading | Exception] = {}
        self.link_calls: list[str] = []
        self.costs: list[tuple[float, int]] = []

    async def on_cost(self, cost_usd: float, n: int) -> None:
        self.costs.append((cost_usd, n))

    async def on_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))

    async def read_files(self, ids: list[str], meter: Meter) -> list[FileReading]:
        """Like the real reader, reports what each file's model call cost through the meter before returning."""
        out = []
        for i in ids:
            meter.gate()
            got = self.files[i]
            cost = self.file_costs.get(i, getattr(got, "cost_usd", 0.0))
            if cost:
                await meter.spend(cost)
            if isinstance(got, Exception):
                raise got
            out.append(got)
        return out

    async def read_link(self, url: str) -> FileReading:
        self.link_calls.append(url)
        got = self.links[url]
        if isinstance(got, Exception):
            raise got
        return got

    async def send(
        self, text: str, file_ids: list[str] | None = None, chat: str = "c1", draft_id: str | None = None
    ) -> dict:
        self.events.clear()
        graph = build_graph(
            self.pack,
            self.llm,
            lambda: self.tools,
            self.saver,
            self.on_event,
            self.read_files,
            self.read_link,
            self.on_cost,
        )
        config = {"configurable": {"thread_id": thread_id(self.pack.id, "ws1", chat)}}
        state = {"messages": [HumanMessage(text)], "file_ids": file_ids or [], "draft_id": draft_id}
        return await graph.ainvoke(state, config)

    def of(self, kind: str) -> list[dict]:
        return [p for k, p in self.events if k == kind]

    def notes(self) -> list[str]:
        return [p["text"] for p in self.of("note")]

    def text(self) -> str:
        return "".join(p["delta"] for p in self.of("text"))


def system_of(call: dict) -> str:
    return call["messages"][0]["content"]


def tool_results(call: dict) -> list[dict]:
    """The `role: tool` messages a model call was given."""
    return [m for m in call["messages"] if m.get("role") == "tool"]
