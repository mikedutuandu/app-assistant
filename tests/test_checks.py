from assistant.checks import Fact, numbers_in, repeats, rule_problems, unsupported_numbers
from assistant.packs import WriteSpec


def spec(**kw) -> WriteSpec:
    return WriteSpec(kind="x_post", count=1, instructions="x", **kw)


def test_numbers_in_brief_case():
    assert numbers_in("40 users, 2h → 2 min, 1.5K views, in 2026") == [40, 2, 2, 1500]


def test_numbers_in_normalisation():
    assert numbers_in("1,500 and $1,200") == [1500, 1200]
    assert numbers_in("2M users, 2 min") == [2_000_000, 2]
    assert numbers_in("40% and 3x and 3×") == [40, 3, 3]
    assert numbers_in("2 hours, 2 hrs") == [2, 2]
    assert numbers_in("2-3 weeks") == [2, 3]
    assert numbers_in("down -5 points, 1.5 stars") == [5, 1.5]
    assert numbers_in("in 1999 and 2100 and 1899") == [1899]
    assert numbers_in("2026% growth") == [2026]
    assert numbers_in("grew 1,950 times") == [1950]


def test_number_words():
    assert numbers_in("forty users") == [40]
    assert numbers_in("Forty-Two and forty two and twelve") == [42, 42, 12]
    assert numbers_in("two hundred and 3 thousand") == [200, 3000]
    assert numbers_in("someone, honest, bone") == []
    assert numbers_in("One thing I learned, no one cares, one of them") == []
    assert numbers_in("1 customer") == [1]
    assert numbers_in("twenty-one and thirty one") == [21, 31]


def test_unsupported_numbers():
    assert unsupported_numbers("1,500 views and 3x faster", [Fact(numbers=[1500])]) == [3]
    assert unsupported_numbers("forty users", [Fact(numbers=[40])]) == []
    assert unsupported_numbers("1,010 views", [Fact(numbers=[1000])]) == [1010]
    assert unsupported_numbers("1,010 views", [Fact(numbers=[1000]), Fact(numbers=[1012])]) == []
    assert unsupported_numbers("no numbers", []) == []
    assert unsupported_numbers("0 sales", [Fact(numbers=[0])]) == []


def test_fact_defaults():
    f = Fact()
    assert f.id and f.text == "" and f.source == "typed" and f.file_id is None and f.numbers == []
    assert Fact().id != Fact().id


def test_repeats():
    recent = ["We shipped the new billing page today and users love it", "Totally other topic about cats"]
    assert repeats("Shipped the new billing page today, and users love it!", recent) == recent[0]
    assert repeats("Learned a lesson about pricing experiments", recent) is None
    assert repeats("a b", ["a b"]) is None


def test_rule_problems():
    assert rule_problems("a" * 281, spec(max_chars=280)) == ["too_long"]
    assert rule_problems("a" * 280, spec(max_chars=280)) == []
    assert rule_problems("a" * 281, spec()) == []


def test_rule_problems_url_counts_23():
    url = "https://example.com/" + "p" * 100
    assert rule_problems("a" * 256 + " " + url, spec(max_chars=280)) == []
    assert rule_problems("a" * 258 + " " + url, spec(max_chars=280)) == ["too_long"]


def test_rule_problems_links_and_dashes():
    s = spec(no_links=True, no_dashes=True)
    assert rule_problems("see example.com/path now", s) == ["has_link"]
    assert rule_problems("see https://x.co", s) == ["has_link"]
    assert rule_problems("well — fine", s) == ["has_dash"]
    assert rule_problems("2–3 weeks", s) == ["has_dash"]
    assert rule_problems("well-known. Then this ends.Fine", s) == []
    assert rule_problems("a" * 300 + " example.com — x", spec(max_chars=100, no_links=True, no_dashes=True)) == [
        "too_long",
        "has_link",
        "has_dash",
    ]
    assert rule_problems("example.com — x", spec()) == []


def test_repeats_unicode():
    vi = ["Hôm nay chúng tôi ra mắt tính năng mới"]
    assert repeats("Chúng tôi ra mắt tính năng mới hôm nay", vi) == vi[0]
    ar = ["أطلقنا اليوم ميزة جديدة في التطبيق"]
    assert repeats("اليوم أطلقنا ميزة جديدة في التطبيق", ar) == ar[0]
    assert repeats("Hôm nay trời đẹp quá", vi) is None


def test_rule_problems_uppercase_urls():
    s = spec(no_links=True)
    assert rule_problems("see EXAMPLE.COM", s) == ["has_link"]
    assert rule_problems("see HTTPS://a.b/c", s) == ["has_link"]
    assert rule_problems("a" * 258 + " EXAMPLE.COM/" + "P" * 50, spec(max_chars=280)) == ["too_long"]
    assert rule_problems("well that was the end.Then more", s) == []
