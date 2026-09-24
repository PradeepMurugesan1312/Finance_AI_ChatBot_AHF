from __future__ import annotations

import json

import pytest

from ahf_finance_agent import knowledge_base as kb
from ahf_finance_agent.config import get_settings


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("KB_BACKEND", "local")
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "index.json"))
    monkeypatch.delenv("EMBEDDING_DEPLOYMENT_ID", raising=False)
    get_settings.cache_clear()
    kb.reset_index_cache()
    yield
    get_settings.cache_clear()
    kb.reset_index_cache()


def test_local_embedding_is_deterministic_and_normalized():
    a = kb.embed_texts(["purchase order approval threshold"])[0]
    b = kb.embed_texts(["purchase order approval threshold"])[0]
    assert a == b
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embedding_backend_name_is_local_without_deployment():
    assert kb.embedding_backend_name() == "local"


def test_chunk_document_tracks_section_and_title():
    text = "# T&E Policy\n\nintro line\n\n## Receipts\n\nAn itemized receipt is required.\n"
    chunks = kb.chunk_document(text, source="te.md", title="T&E Policy")
    assert any(c.section == "Receipts" for c in chunks)
    assert all(c.title == "T&E Policy" and c.source == "te.md" for c in chunks)


def _build(docs):
    chunks = []
    for src, title, body in docs:
        chunks.extend(kb.chunk_document(body, source=src, title=title))
    kb.VectorIndex().build(chunks)


def test_retrieve_grounded_vs_not_grounded():
    _build([
        ("po.md", "PO Approval Thresholds",
         "# PO Approval Thresholds\n\n## Limits\n\nA purchase order over 100000 "
         "requires CFO approval. Splitting a purchase to dodge a threshold is "
         "prohibited.\n"),
        ("te.md", "Travel and Expense Policy",
         "# Travel and Expense Policy\n\n## Submission deadline\n\nExpense "
         "reports must be submitted within 30 days of the trip end date.\n"),
    ])
    idx = kb.get_index()

    hits, grounded = idx.retrieve("what is the purchase order approval threshold")
    assert grounded
    assert hits[0].title == "PO Approval Thresholds"

    hits, grounded = idx.retrieve("what is the capital of France")
    assert not grounded


def test_retrieve_empty_index_is_not_grounded():
    hits, grounded = kb.get_index().retrieve("anything at all")
    assert hits == []
    assert grounded is False


def test_index_file_records_embedding_backend_and_chunks(tmp_path):
    _build([("a.md", "A", "# A\n\n## S\n\nsome content about invoice payment terms\n")])
    lines = [l for l in (tmp_path / "index.json").read_text().splitlines() if l.strip()]
    header = json.loads(lines[0])
    first_chunk = json.loads(lines[1])
    assert header["embedding_backend"] == "local"
    assert "embedding" in first_chunk and first_chunk["source"] == "a.md"
    assert kb.get_index().chunk_count() == 1
