import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    # Fresh app (and fresh in-memory store) per test: no cross-test state.
    return TestClient(create_app(), raise_server_exceptions=False)


def assert_error_envelope(response, status: int) -> dict:
    """Every 4xx/5xx must carry {"error": str, "code": int} with code == HTTP status."""
    assert response.status_code == status
    body = response.json()
    assert isinstance(body["error"], str) and body["error"]
    assert body["code"] == status
    return body
