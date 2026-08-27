from __future__ import annotations


def test_health_is_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ready_reports_unwired_dependencies(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    deps = resp.json()["dependencies"]
    # Step 1: nothing downstream is wired yet, and that is reported honestly.
    assert deps["llm_configured"] is False
    assert deps["task_store"] == "in-memory"
