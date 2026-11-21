"""The chat model's tools are written twice: as JSON for the model (`tool_specs`) and as Pydantic models that check a
call's arguments. These tests keep the two in step."""

from graph_harness import make_pack
from pydantic import BaseModel

from assistant.graph.agent_tools import (
    TOOLS,
    FactArg,
    ReviseDraftArgs,
    SaveFactsArgs,
    WriteDraftsArgs,
    prepare_calls,
    tool_specs,
)
from assistant.llm import ToolCall

CHECKS: dict[str, type[BaseModel]] = {
    "write_drafts": WriteDraftsArgs,
    "revise_draft": ReviseDraftArgs,
    "save_facts": SaveFactsArgs,
}


def _spec(name: str) -> dict:
    [spec] = [s["function"]["parameters"] for s in tool_specs(make_pack()) if s["function"]["name"] == name]
    return spec


def _required(model: type[BaseModel]) -> set[str]:
    return {n for n, f in model.model_fields.items() if f.is_required()}


def test_every_tool_the_model_is_told_about_has_a_check_and_a_name():
    assert [s["function"]["name"] for s in tool_specs(make_pack())] == list(TOOLS) == list(CHECKS)


def test_the_json_and_the_checks_name_the_same_arguments():
    for name, model in CHECKS.items():
        spec = _spec(name)
        assert set(spec["properties"]) == set(model.model_fields), name
        # What the check requires, the model is told is required (the JSON may ask for more, e.g. `language`).
        assert _required(model) <= set(spec["required"]), name
    item = _spec("save_facts")["properties"]["facts"]["items"]
    assert set(item["properties"]) == set(FactArg.model_fields)
    assert _required(FactArg) <= set(item["required"])


def test_prepare_calls_answers_bad_calls_and_keeps_ids_unique():
    used: set[str] = set()
    calls = [
        ToolCall(id="a", name="save_facts", arguments={"facts": [{"text": "x"}]}),
        ToolCall(id="a", name="write_drafts", arguments={}),  # the same id twice
        ToolCall(id="", name="save_facts", arguments={}),
        ToolCall(id="c", name="post_it", arguments={}),
    ]
    out = prepare_calls(calls, 1, used)
    assert [c.id for c in out] == ["a", "bot_1_2", "bot_1_3", "c"]
    assert out[0].problem is None and out[1].problem is None
    assert "no id" in (out[2].problem or "")
    assert "unknown tool 'post_it'" in (out[3].problem or "")
    assert used == {"a", "bot_1_2", "bot_1_3", "c"}
