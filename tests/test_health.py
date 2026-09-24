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
    # Step 4: the policy KB status is reported too.
    assert deps["kb_backend"] == "local"
    assert deps["kb_embedding_backend"] in ("local", "aicore", "?")
    assert isinstance(deps["kb_index_chunks"], int)
    assert deps["finance_domains"] >= 10
    assert deps["finance_domains_live"] >= 1


def test_diag_domains_lists_full_finance_scope(client):
    body = client.get("/diag/domains").json()
    assert body["summary"]["total"] >= 10
    keys = {d["key"] for d in body["domains"]}
    assert {"accounts_receivable", "fixed_assets", "tax", "bank_cash"} <= keys
    for d in body["domains"]:
        assert d["status"] in ("live", "kb_only", "planned")


def test_diag_kb_reports_index_and_sample_retrieval(client):
    resp = client.get("/diag/kb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kb_backend"] == "local"
    assert "grounded" in body and "top_hits" in body


def test_diag_s4_reports_error_when_destination_unbound(client):
    # No destination-service bound and no DESTINATION_SERVICE_KEY in tests, so
    # the smoke call fails cleanly rather than 500-ing.
    resp = client.get("/diag/s4")
    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False
    assert "S43" in body["error"] or "destination" in body["error"].lower()


def test_startup_catalogue_warmup_does_not_break_boot(client):
    # Entering the lifespan runs _warm_s4_catalog; with no destination bound it
    # must log-and-continue, never fail startup.
    with client:
        assert client.get("/health").json()["status"] == "ok"


def test_diag_s4_catalog_lists_every_capability_and_marks_unresolved(client):
    # No destination bound -> every capability probe is unreachable, so the
    # catalogue is fully unresolved but the endpoint still answers 200 with the
    # per-capability breakdown.
    resp = client.get("/diag/s4/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    caps = body["capabilities"]
    assert {"supplier_invoice_item", "goods_receipt_item", "purchase_requisition_header"} <= set(caps)
    assert set(body["unresolved"]) == set(caps)
    assert all(v["ok"] is False for v in caps.values())
    # each candidate carries a status + a human detail explaining why
    some = caps["purchase_order_schedule_line"]["candidates"][0]
    assert some["status"] in ("absent", "unreachable")
    assert some["detail"]  # non-empty reason string
