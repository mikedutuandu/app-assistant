from assistant.llm import _cost


def test_cost_on_openrouter_credits():
    assert _cost({"cost": 0.05}) == 0.05
    assert _cost({"cost": 0.05, "is_byok": False, "cost_details": {"upstream_inference_cost": 0.05}}) == 0.05


def test_cost_on_our_own_key_adds_the_providers_charge():
    assert _cost({"cost": 0, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.08}}) == 0.08
    assert (
        abs(_cost({"cost": 0.004, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.08}}) - 0.084) < 1e-12
    )


def test_cost_never_loses_an_upstream_cost_above_the_charge():
    assert _cost({"cost": 0, "cost_details": {"upstream_inference_cost": 0.08}}) == 0.08


def test_cost_is_zero_when_nothing_says():
    assert _cost(None) == 0.0
    assert _cost({}) == 0.0
    assert _cost({"cost_details": None}) == 0.0
