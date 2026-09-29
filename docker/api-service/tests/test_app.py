"""Unit tests for the CzarIntel API service (run: pytest -q docker/api-service/tests)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

import app as api  # noqa: E402

client = TestClient(api.app)


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == api.APP_VERSION
    assert "timestamp" in body
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_list_services_counts_are_consistent():
    body = client.get("/api/v1/services").json()
    assert body["count"] == len(body["services"])
    assert body["healthy"] + body["degraded"] + body["down"] == body["count"]


def test_filter_by_cloud():
    body = client.get("/api/v1/services", params={"cloud": "gcp"}).json()
    assert body["count"] >= 1
    assert all(s["cloud"] == "gcp" for s in body["services"])


def test_get_unknown_service_returns_404():
    resp = client.get("/api/v1/services/does-not-exist")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_ready_without_db_is_ready():
    assert client.get("/ready").json()["status"] == "ready"


def test_metrics_exposes_request_counters():
    client.get("/health")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    assert 'path="/health"' in resp.text
    assert "czarintel_service_up" in resp.text


def test_request_id_is_propagated():
    resp = client.get("/health", headers={"x-request-id": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"
