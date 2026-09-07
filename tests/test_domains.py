from __future__ import annotations

from pathlib import Path

from ahf_finance_agent.config import get_settings
from ahf_finance_agent.domains import DOMAINS_BY_KEY, FINANCE_DOMAINS, coverage_summary

_VALID_STATUS = {"live", "kb_only", "planned"}


def test_registry_covers_the_image_list():
    # The finance areas the agent must be trained to answer (from the design image).
    for area in (
        "general_ledger", "gl_accounts_balances", "accounts_payable",
        "accounts_receivable", "cost_centers", "profit_centers",
        "fixed_assets", "bank_cash", "tax", "payments",
    ):
        assert area in DOMAINS_BY_KEY, area


def test_every_domain_is_well_formed():
    for d in FINANCE_DOMAINS:
        assert d.status in _VALID_STATUS
        assert d.name and d.sap_area
        assert d.example_questions
        assert d.odata_services


def test_kb_docs_referenced_by_domains_exist_on_disk():
    docs_dir = Path(get_settings().kb_docs_dir)
    for d in FINANCE_DOMAINS:
        for doc in d.kb_docs:
            assert (docs_dir / doc).is_file(), f"{d.key} -> missing {doc}"


def test_coverage_summary_partitions_all_domains():
    cov = coverage_summary()
    assert cov["total"] == len(FINANCE_DOMAINS)
    assert len(cov["live"]) + len(cov["kb_only"]) + len(cov["planned"]) == cov["total"]
    # The connected-today set matches what tools.py actually wires.
    assert set(cov["live"]) == {
        "accounts_payable", "procurement", "goods_receipt", "vendor_master", "payments",
        "invoice_verification", "general_ledger", "gl_accounts_balances",
        "cost_centers", "profit_centers",
    }
