from fastapi.testclient import TestClient

from app.core.broadcast import broadcast
from app.core.config import settings
from app.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_spa_root_and_static_mount_are_served() -> None:
    with TestClient(app) as client:
        root_response = client.get("/")
        static_response = client.get("/static/.gitkeep")

    assert root_response.status_code == 200
    assert '<div id="root"></div>' in root_response.text
    assert 'id="fastapi-config"' in root_response.text
    assert static_response.status_code == 200


def test_default_startup_uses_process_local_broadcast_backend() -> None:
    assert settings.broadcast_url == "memory://"
    assert type(broadcast._backend).__name__ == "MemoryBackend"

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
