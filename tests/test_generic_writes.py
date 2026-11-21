"""The bot writes whatever an app declares, not only posts for X (Mike, 21 Nov 2026): a changelog entry, a LinkedIn
post, a product description. An app's pack names each kind it writes; the tool, the prompts and the writer's checks
come from the pack, and nothing in them names a platform."""

import re

import pytest
from fakes import FakeLlm
from pydantic import ValidationError
from test_writer import FACTS, OK, deps, drafts, make_pack

from assistant.graph import prompts
from assistant.graph.agent_tools import tool_specs
from assistant.packs import WriteSpec
from assistant.writer import write_drafts

ENTRY = WriteSpec(
    kind="changelog_entry",
    name="Changelog entry",
    about="an entry for the product's public changelog",
    max_chars=600,
    instructions="A short entry: what changed and who it helps.",
)
CAROUSEL = WriteSpec(
    kind="linkedin_carousel",
    name="LinkedIn carousel",
    parts="several",
    max_posts=3,
    instructions="One slide per point.",
)


def changelog_pack():
    return make_pack().model_copy(update={"writes": [ENTRY, CAROUSEL]})


# ---- the pack ----


def test_an_app_names_any_kind_it_writes():
    assert ENTRY.parts == "one" and ENTRY.name == "Changelog entry"
    assert CAROUSEL.parts == "several"
    with pytest.raises(ValidationError, match="name"):
        WriteSpec(kind="changelog_entry", instructions="x")  # a kind of its own needs a name
    with pytest.raises(ValidationError):
        WriteSpec(kind="Changelog Entry!", name="x", instructions="x")  # an id: lowercase, digits and _


def test_x_kinds_keep_working_with_nothing_new_in_the_pack():
    post = WriteSpec(kind="x_post", instructions="A post.")
    thread = WriteSpec(kind="x_thread", instructions="A thread.")
    assert (post.name, post.parts) == ("X post", "one")
    assert (thread.name, thread.parts) == ("X thread", "several")
    assert "asks for a thread" in (thread.about or "")


# ---- what the model is told ----


def test_the_write_tool_offers_the_packs_kinds_by_name():
    write = next(s["function"] for s in tool_specs(changelog_pack()) if s["function"]["name"] == "write_drafts")
    kind = write["parameters"]["properties"]["kind"]
    assert kind["enum"] == ["changelog_entry", "linkedin_carousel"]
    assert (
        "Changelog entry" in kind["description"]
        and "an entry for the product's public changelog" in kind["description"]
    )
    text = str(write)
    assert "x_post" not in text and not re.search(r"\bX\b", text)


def test_no_prompt_names_a_platform():
    for name in ("CONVERSE", "DRAFT_WRITER", "CLAIM_CHECK", "LANGUAGE_RULE"):
        text = getattr(prompts, name)
        assert not re.search(r"\bX\b|x_post|x_thread|tweet", text), name


# ---- the writer ----


async def test_the_writer_writes_a_kind_of_the_apps_own():
    entry = "Auto-reply now answers in 2 minutes. Solo founders keep up with every reply."
    llm = FakeLlm({"DRAFT_WRITER": [drafts([entry])], "CLAIM_CHECK": [OK]})
    w, _ = deps(llm, pack=changelog_pack())
    result = await write_drafts(w, "the auto-reply launch", "changelog_entry", 1)
    assert [(d.kind, d.posts) for d in result.drafts] == [("changelog_entry", [entry])]
    writer = llm.calls[0]["messages"][0]["content"] + llm.calls[0]["messages"][1]["content"]
    assert "Changelog entry" in writer and "at most 600 characters" in writer


async def test_a_several_part_kind_keeps_its_parts_and_their_limit():
    parts = ["Auto-reply now answers in 2 minutes.", "It helps solo founders keep up with replies."]
    llm = FakeLlm({"DRAFT_WRITER": [drafts(parts)], "CLAIM_CHECK": [OK]})
    w, _ = deps(llm, pack=changelog_pack())
    result = await write_drafts(w, "the auto-reply launch", "linkedin_carousel", 1)
    assert result.drafts[0].posts == parts
    assert "2 to 3 parts" in llm.calls[0]["messages"][0]["content"] + llm.calls[0]["messages"][1]["content"]


async def test_a_kind_the_app_doesnt_write_is_refused_with_its_kinds():
    w, _ = deps(FakeLlm(), pack=changelog_pack())
    result = await write_drafts(w, "launch", "x_post", 1)
    assert result.drafts == [] and "changelog_entry, linkedin_carousel" in (result.note or "")
    assert FACTS  # the facts were there: only the kind was refused
