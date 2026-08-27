from __future__ import annotations

from ahf_finance_agent.logging_setup import redact


def test_redacts_iban():
    assert "DE89" not in redact("pay to DE89 3704 0044 0532 0130 00 now")


def test_redacts_email_and_ssn():
    out = redact("contact jane.doe@vendor.com ssn 123-45-6789")
    assert "jane.doe@vendor.com" not in out
    assert "123-45-6789" not in out


def test_leaves_ordinary_text_alone():
    text = "invoice 5105601234 for PO 4500001234 is blocked"
    assert redact(text) == text
