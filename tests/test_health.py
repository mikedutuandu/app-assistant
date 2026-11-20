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


def test_assistant_logs_go_to_stdout_and_the_root_logger_is_left_alone(test_settings, capsys):
    import logging

    root = logging.getLogger()
    root_before = (list(root.handlers), root.level)
    create_app(test_settings)
    create_app(test_settings)  # a second app adds no second handler
    logging.getLogger("assistant.api").info("action app=real thread=t1 action=keep_facts")
    logging.getLogger("httpx").info("HTTP Request: GET https://example.test/secret-path")
    out = capsys.readouterr().out
    assert out.count("INFO assistant.api action app=real thread=t1 action=keep_facts\n") == 1
    assert "secret-path" not in out
    assert (list(root.handlers), root.level) == root_before
    assert logging.getLogger("assistant").propagate is False
