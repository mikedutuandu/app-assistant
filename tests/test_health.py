from fastapi.testclient import TestClient

from assistant.main import create_app


def test_health_ok(test_settings):
    client = TestClient(create_app(test_settings))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
