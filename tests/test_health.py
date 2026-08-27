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
    assert deps["s4hana_destination"] == "S43"


def test_diag_s4_reports_error_when_destination_unbound(client):
    # No destination-service bound and no DESTINATION_SERVICE_KEY in tests, so
    # the smoke call fails cleanly rather than 500-ing.
    resp = client.get("/diag/s4")
    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False
    assert "S43" in body["error"] or "destination" in body["error"].lower()
