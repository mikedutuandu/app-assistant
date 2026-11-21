"""A post with the user's picture (any image they shared, chosen by the conversation model) and a time to post it
(one of the app's free times), as the chat shows them."""

from fakes import FakeLlm, FakeTools, Reply, call, default_context
from graph_harness import Harness, make_pack, system_of, tool_results

from assistant.files.read import FileReading
from assistant.packs import WriteSpec

FACT = "We moved into a new office."
POST = "We moved into a new office."
OK = {"unsupported": []}
TIMES = [
    {"at": "2026-11-24T02:30:00Z", "label": "Tue 9:30 AM", "why": "one of your best hours"},
    {"at": "2026-11-25T12:00:00Z", "label": "Wed 7:00 PM"},
]
PHOTO = FileReading(file_id="img1", kind="image", text="A room with desks and plants.")


def written(*posts: str) -> dict:
    return {"drafts": [{"posts": [p], "fact_ids": ["F1"]} for p in posts]}


def write(**extra) -> dict:
    return call("write_drafts", brief="the new office", kind="x_post", language="en", **extra)


def told(llm: FakeLlm) -> str:
    """What the last tool call answered, as the conversation model read it."""
    return tool_results([c for c in llm.calls if c["name"] == "CONVERSE"][-1])[-1]["content"]


def harness(*replies, tools: FakeTools | None = None, pack=None, writer=None) -> tuple[Harness, FakeLlm]:
    llm = FakeLlm(
        {
            "CONVERSE": [
                Reply(calls=[call("save_facts", facts=[{"text": FACT}]), write(**(replies[0] if replies else {}))]),
                "Here it is.",
                *replies[1:],
            ],
            "DRAFT_WRITER": writer or [written(POST)],
            "CLAIM_CHECK": [OK, OK],
        }
    )
    h = Harness(llm, tools=tools, pack=pack)
    h.files["img1"] = PHOTO
    return h, llm


# ---- the picture ----


async def test_a_picture_the_user_sent_goes_on_the_post_without_a_fact_from_it():
    h, _ = harness({"image_file_id": "img1"})
    await h.send("here's our new office, post it with this photo", file_ids=["img1"])
    [draft] = h.of("draft")
    assert draft["image_file_id"] == "img1"


async def test_no_picture_unless_the_model_names_one():
    h, _ = harness({})
    await h.send("post about our new office", file_ids=["img1"])
    [draft] = h.of("draft")
    assert "image_file_id" not in draft


async def test_a_file_that_is_not_a_shared_image_is_not_attached_and_the_model_is_told():
    h, llm = harness({"image_file_id": "nope"})
    await h.send("post about our new office", file_ids=["img1"])
    [draft] = h.of("draft")
    assert "image_file_id" not in draft
    assert "not an image the user shared in this chat" in told(llm)


async def test_a_kind_without_pictures_carries_none():
    rules = {"max_chars": 280, "no_links": True, "no_dashes": True, "may_carry_image": False}
    pack = make_pack()
    pack.writes = [WriteSpec(kind="x_post", instructions="A short post.", **rules)]
    h, llm = harness({"image_file_id": "img1"}, pack=pack)
    await h.send("post it with this photo", file_ids=["img1"])
    [draft] = h.of("draft")
    assert "image_file_id" not in draft
    assert "can't carry a picture" in told(llm)


async def test_the_writer_is_not_asked_to_pick_an_image():
    h, llm = harness({"image_file_id": "img1"})
    await h.send("post it with this photo", file_ids=["img1"])
    writer = next(c for c in llm.calls if c["name"] == "DRAFT_WRITER")
    assert all("image_file_id" not in m["content"] for m in writer["messages"])


def revise(draft_id: str, **extra) -> Reply:
    return Reply(calls=[call("revise_draft", draft_id=draft_id, instruction="shorter", language="en", **extra)])


async def test_a_revision_keeps_the_picture():
    h, llm = harness({"image_file_id": "img1"}, writer=[written(POST), written("New office!")])
    await h.send("post it with this photo", file_ids=["img1"])
    [first] = h.of("draft")
    llm.add("CONVERSE", revise(first["id"]), "Shorter.")
    await h.send("shorter")
    [second] = h.of("draft")
    assert second["image_file_id"] == "img1"


async def test_revise_draft_can_add_the_picture():
    h, llm = harness({}, writer=[written(POST), written("New office!")])
    await h.send("post about our new office", file_ids=["img1"])
    [first] = h.of("draft")
    llm.add("CONVERSE", revise(first["id"], image_file_id="img1"), "Done.")
    await h.send("yes add my photo")
    [second] = h.of("draft")
    assert second["image_file_id"] == "img1"


# ---- the time ----


def timed_tools() -> FakeTools:
    return FakeTools(context=default_context(post_times=TIMES))


async def test_the_app_free_times_are_in_the_system_prompt():
    h, llm = harness({}, tools=timed_tools())
    await h.send("post about our new office")
    system = system_of(llm.calls[0])
    assert "Tue 9:30 AM" in system and "2026-11-24T02:30:00Z" in system and "one of your best hours" in system


async def test_no_free_times_no_times_section():
    h, llm = harness({})
    await h.send("post about our new office")
    assert "Free times" not in system_of(llm.calls[0])


async def test_a_draft_carries_the_time_the_model_picked_and_the_choices():
    h, _ = harness({"post_at": "2026-11-24T02:30:00Z"}, tools=timed_tools())
    await h.send("post about our new office")
    [draft] = h.of("draft")
    assert draft["post_at"] == "2026-11-24T02:30:00Z"
    assert draft["post_times"] == [{"at": t["at"], "label": t["label"]} for t in TIMES]


async def test_a_time_that_is_not_free_is_dropped_and_the_model_is_told():
    h, llm = harness({"post_at": "2026-12-01T00:00:00Z"}, tools=timed_tools())
    await h.send("post about our new office")
    [draft] = h.of("draft")
    assert "post_at" not in draft and draft["post_times"]
    assert "post_at is not one of the app's free times" in told(llm)


async def test_without_free_times_a_draft_has_none():
    h, _ = harness({"post_at": "2026-11-24T02:30:00Z"})
    await h.send("post about our new office")
    [draft] = h.of("draft")
    assert "post_at" not in draft and "post_times" not in draft


async def test_a_revision_keeps_the_time():
    h, llm = harness(
        {"post_at": "2026-11-25T12:00:00Z"}, tools=timed_tools(), writer=[written(POST), written("New office!")]
    )
    await h.send("post about our new office")
    [first] = h.of("draft")
    llm.add("CONVERSE", revise(first["id"]), "Shorter.")
    await h.send("shorter")
    [second] = h.of("draft")
    assert second["post_at"] == "2026-11-25T12:00:00Z"
