from __future__ import annotations

from ahf_finance_agent.guardrails import scrub_response, strip_sensitive_keys


def test_scrub_response_does_not_redact_iban():
    # Deliberate exception (2026-09): get_vendor_bank_accounts is allowed to
    # surface a vendor's IBAN, so the text-level scrub leaves it alone
    # (field-level SENSITIVE_KEYS still blocks IBAN on every other lookup).
    text, reasons = scrub_response("Wire to DE89370400440532013000 please.")
    assert "DE89370400440532013000" in text
    assert "iban" not in reasons


def test_scrub_response_does_not_redact_bank_account_near_keyword():
    text, reasons = scrub_response("Bank account number: 1234567890123 for payment.")
    assert "1234567890123" in text
    assert "bank-account" not in reasons


def test_scrub_response_redacts_ssn_or_tax_id():
    text, reasons = scrub_response("SSN on file is 123-45-6789.")
    assert "123-45-6789" not in text
    assert "ssn-or-tax-id" in reasons


def test_scrub_response_redacts_valid_card_number_only():
    # Luhn-valid test card number -> redacted
    text, reasons = scrub_response("Card 4111111111111111 was charged.")
    assert "4111111111111111" not in text
    assert "card-number" in reasons


def test_scrub_response_leaves_luhn_invalid_digit_strings_alone():
    # A long digit string that fails the Luhn check is probably an invoice /
    # document number, not a real card -- must not be redacted.
    text, reasons = scrub_response("Invoice reference 1234567890123456 was posted.")
    assert "1234567890123456" in text
    assert "card-number" not in reasons


def test_scrub_response_does_not_redact_email_addresses():
    # Deliberate exception (2026-09): get_vendor_email_addresses is allowed to
    # surface vendor/contact emails, so the text-level scrub leaves them alone.
    text, reasons = scrub_response("Contact ap.team@vendor.com for questions.")
    assert "ap.team@vendor.com" in text
    assert "email-address" not in reasons


def test_scrub_response_empty_text_is_a_noop():
    assert scrub_response("") == ("", [])


def test_scrub_response_dedupes_reasons():
    text, reasons = scrub_response("SSNs 123-45-6789 and 987-65-4321 were both used.")
    assert reasons.count("ssn-or-tax-id") == 1


def test_strip_sensitive_keys_drops_bank_and_email_case_insensitively():
    record = {
        "SupplierInvoice": "5100000016",
        "BankAccount": "12345678",
        "emailaddress": "james.smith@example.com",
        "TaxNumber1": "AB123",
    }
    cleaned = strip_sensitive_keys(record)
    assert cleaned == {"SupplierInvoice": "5100000016"}


def test_strip_sensitive_keys_recurses_into_lists_and_nested_dicts():
    record = {
        "items": [
            {"GLAccount": "1000", "IBAN": "DE89370400440532013000"},
            {"GLAccount": "2000", "PhoneNumber": "+1-555-0100"},
        ],
    }
    cleaned = strip_sensitive_keys(record)
    assert cleaned == {"items": [{"GLAccount": "1000"}, {"GLAccount": "2000"}]}


def test_strip_sensitive_keys_leaves_non_sensitive_fields_untouched():
    record = {"PurchaseOrder": "4500000030", "Supplier": "USSU-VSF04"}
    assert strip_sensitive_keys(record) == record
