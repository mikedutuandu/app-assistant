"""A long answer from an app's tool, shortened to what the model reads (Mike, 21 Nov 2026: Ask SiteSeed's list of 3
sites was ~180,000 characters, cut at 12,000 in the middle of the first site, so the model never saw an id). A JSON
answer stays JSON: long texts are shortened first, then long lists keep their first items, and the model is told what
was left out and how to see more."""

import json

from assistant.app_actions import RESULT_CHARS, fit_answer, result_of


def site(i: int) -> dict:
    return {
        "id": f"site-{i}",
        "name": f"Site {i}",
        "description": "d" * 3000,
        "pages": [{"title": f"Page {j} of site {i}", "url": f"https://s{i}.example/p{j}"} for j in range(260)],
    }


def test_a_short_answer_is_left_as_it_is():
    text = json.dumps({"data": [{"id": "a"}]})
    assert fit_answer(text) == text


def test_long_texts_are_shortened_before_any_item_is_dropped():
    text = json.dumps({"data": [{"id": "a", "html": "<p>" + "x" * 20_000 + "</p>"}]})
    fitted = fit_answer(text)
    assert len(fitted) <= RESULT_CHARS
    value, note = fitted.split("\n(", 1)
    data = json.loads(value)["data"]
    assert data[0]["id"] == "a" and data[0]["html"].endswith("…") and len(data[0]["html"]) < 2100
    assert "long texts are shortened" in note


def test_a_long_list_keeps_its_first_items_and_every_id_shown_is_whole():
    text = json.dumps({"total": 3, "data": [site(i) for i in range(3)]})
    assert len(text) > 50_000
    fitted = fit_answer(text)
    assert len(fitted) <= RESULT_CHARS
    value, note = fitted.split("\n(", 1)
    data = json.loads(value)["data"]
    # Every site is still there, whole where it counts: its id and name.
    assert [s["id"] for s in data] == ["site-0", "site-1", "site-2"]
    assert [s["name"] for s in data] == ["Site 0", "Site 1", "Site 2"]
    # Each site's 260 pages: the first kept, the rest counted for the model.
    assert 0 < len(data[0]["pages"]) < 260
    assert "data[].pages: showing" in note and "of 260" in note
    assert "limit" in note  # how to see more


def test_a_list_of_many_items_is_cut_from_the_end_and_counted():
    text = json.dumps([{"id": f"p{i}", "text": "word " * 40} for i in range(500)])
    fitted = fit_answer(text)
    assert len(fitted) <= RESULT_CHARS
    value, note = fitted.split("\n(", 1)
    items = json.loads(value)
    assert items[0]["id"] == "p0" and items[-1]["id"] == f"p{len(items) - 1}"
    assert f"showing {len(items)} of 500" in note


def test_text_that_isnt_json_is_cut_with_a_note():
    fitted = fit_answer("line\n" * 10_000)
    assert len(fitted) <= RESULT_CHARS + 200
    assert fitted.endswith("(The answer was cut here: it was too long. Ask for less, or for one item.)")


def test_a_tools_long_answer_reaches_the_model_fitted():
    body = {
        "content": [{"type": "text", "text": json.dumps({"data": [site(i) for i in range(2)]})}],
    }
    result = result_of(body)
    assert len(result.text) <= RESULT_CHARS
    assert json.loads(result.text.split("\n(", 1)[0])["data"][1]["id"] == "site-1"
