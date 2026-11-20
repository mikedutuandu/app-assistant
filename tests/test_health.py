from fastapi.testclient import TestClient

from assistant.main import create_app


def test_health_ok(test_settings):
    client = TestClient(create_app(test_settings))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_widget_js_is_served_with_a_short_cache(test_settings):
    from assistant.main import WIDGET_JS

    assert WIDGET_JS.exists(), "run `npm run build` in widget/ first"
    response = TestClient(create_app(test_settings)).get("/widget.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.content == WIDGET_JS.read_bytes()
