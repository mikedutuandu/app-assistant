"""The conversation tests (tests/conversations/): the cases are well formed, and every recorded case replays through
the whole bot with its `hard` rules holding. Recording needs the real models: `scripts/converse_eval.py --live
--record` (README, "Conversation tests")."""

import os

import pytest
from converse_eval import (
    CASES,
    FIXTURES,
    JUDGE_MODEL,
    RECORDED,
    ReplayLlm,
    check_hard,
    context_for,
    load_pack,
    load_recording,
    load_suite,
    recording_problems,
    run_case,
)

from assistant.db import migrate
from assistant.files.intake import check_content

SUITE = load_suite(CASES)
BY_NAME = {c.name: c for c in SUITE.cases}
RECORDINGS = sorted(RECORDED.glob("*.json"))
# CI sets this once the first live run is recorded: from then on an empty recorded/ fails instead of skipping.
REQUIRE_RECORDINGS = os.environ.get("REQUIRE_CONVERSATION_RECORDINGS", "") == "1"


# ---- the cases ----


def test_about_twenty_cases_with_notes_and_rules():
    assert 18 <= len(SUITE.cases) <= 25
    for case in SUITE.cases:
        assert case.expect and all(note.strip() for note in case.expect), case.name
        assert all(t.text.strip() or t.file for t in case.turns), case.name
        assert case.turns[0].draft is None, f"{case.name}: the first turn can't be about a draft"


def test_the_context_is_mike_with_two_recent_posts_and_no_kept_facts():
    ctx = SUITE.context
    assert ctx.name == "Mike" and len(ctx.recent_posts) == 2 and ctx.kept_facts == []
    assert ctx.credits_left is None and ctx.language == "en"


def _texts(name: str) -> list[str]:
    return [t.text for t in BY_NAME[name].turns]


def test_mikes_chats_are_all_there():
    """The chats the first tester saw go wrong on 20 Nov 2026, and the rest of the cases."""
    assert _texts("hi") == ["hi"]
    assert _texts("how_are_you")[-1] == "how are you"
    assert _texts("my_name_mike")[-1] == "my name mike"
    assert _texts("playback_chatbot") == ["i deployed chat bot help user plan weet"]
    assert _texts("just_write_it_thin") == ["i deployed chat bot help user plan weet", "just write it"]
    shorter = BY_NAME["shorter_after_draft"].turns[-1]
    assert (shorter.text, shorter.draft) == ("shorter", "last")
    assert BY_NAME["shorter_after_draft"].hard.shorter_than_previous_draft
    assert any(t.file for t in BY_NAME["screenshot_number"].turns)
    assert BY_NAME["screenshot_number"].hard.said == ["184"]
    assert BY_NAME["vietnamese"].hard.language == "vi"
    assert BY_NAME["product_question"].hard.drafts == 0
    assert _texts("give_me_a_thread")[-1] == "give me a thread"
    assert BY_NAME["give_me_a_thread"].hard.draft_kind == "x_thread"
    assert _texts("thanks_after_draft")[-1] == "thanks" and BY_NAME["thanks_after_draft"].hard.drafts == 0
    assert _texts("say_10k_users")[-1] == "say we have 10k users"
    assert "10k" in BY_NAME["say_10k_users"].hard.never_kept


def test_every_fixture_is_a_file_the_pack_takes():
    pack = load_pack()
    for case in SUITE.cases:
        for t in case.turns:
            if t.file:
                data = (FIXTURES / t.file).read_bytes()
                assert check_content(pack, data)[0] == "image", t.file
                assert t.shows, f"{case.name}: say what {t.file} shows, for the judge"


def test_the_models_are_the_packs_and_the_judge_is_sonnet():
    models = load_pack().models
    assert (models.chat, models.chat_reasoning) == ("anthropic/claude-sonnet-5.5", "low")
    assert (models.writer, models.writer_reasoning) == ("anthropic/claude-sonnet-5.5", "low")
    assert JUDGE_MODEL == "anthropic/claude-sonnet-5.5"


# ---- the recordings ----


@pytest.mark.parametrize("path", RECORDINGS, ids=[p.stem for p in RECORDINGS])
async def test_a_recorded_case_replays_with_its_hard_rules(path, pool):
    recording = load_recording(path)
    case = BY_NAME.get(recording["case"])
    assert case is not None, f"{path.name}: no case named {recording['case']!r} in cases.yaml: delete the recording"
    assert recording_problems(recording, case, load_pack()) == []  # same case, same models, every note held
    await migrate(pool)
    llm = ReplayLlm(recording["calls"])
    logs = await run_case(case, context_for(SUITE, case), llm, pool)
    assert llm.left == 0, f"{case.name}: {llm.left} recorded calls were not made: record the case again"
    assert check_hard(case, logs) == []


@pytest.mark.skipif(
    not RECORDINGS and not REQUIRE_RECORDINGS,
    reason="no recordings yet: scripts/converse_eval.py --live --record (REQUIRE_CONVERSATION_RECORDINGS=1 to fail)",
)
def test_every_case_has_a_recording():
    recorded = {p.stem for p in RECORDINGS}
    assert sorted(set(BY_NAME) - recorded) == [], "cases with no recording"
    assert sorted(recorded - set(BY_NAME)) == [], "recordings with no case"
