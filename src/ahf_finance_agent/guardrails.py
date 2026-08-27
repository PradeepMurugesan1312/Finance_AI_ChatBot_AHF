"""Output guardrails.

The design brief is explicit: no PII or vendor banking data in responses. Two
layers defend that:

1. Field-level — the S/4HANA ``$select`` lists (build step 3) never request a
   sensitive field, and :func:`strip_sensitive_keys` drops any that appear in
   a tool result anyway.
2. Text-level — :func:`scrub_response` runs over the final answer string just
   before it goes back to Joule, redacting anything that still looks like an
   IBAN, bank account, card number, SSN/tax id, or email.

:func:`scrub_response` is conservative about false positives — redacting a
legitimate invoice number would be worse than the rare redaction of an
already access-controlled value that slipped through.
"""

from __future__ import annotations

import re

REDACTION = "[redacted]"

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_BANK_NEAR_KEYWORD = re.compile(
    r"(?i)\b(?:bank\s*acct|bank\s*account|account\s*(?:no|number|#)|routing|sort\s*code|swift|bic|iban)\b"
    r"[^\n:]{0,20}[:#]?\s*([A-Z0-9][A-Z0-9 -]{6,34}[A-Z0-9])"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

SENSITIVE_KEYS = frozenset(
    k.lower()
    for k in (
        "BankAccount", "BankAccountNumber", "BankNumber", "BankControlKey",
        "IBAN", "SWIFTCode", "BankAccountHolderName", "PaymentCardNumber",
        "TaxNumber1", "TaxNumber2", "TaxNumber3", "TaxNumber4", "TaxNumber5",
        "SocialInsuranceNumber", "BusinessPartnerIDNumber",
        "PersonFullName", "FirstName", "LastName", "PhoneNumber",
        "MobilePhoneNumber", "EmailAddress", "HomeAddress",
    )
)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def scrub_response(text: str) -> tuple[str, list[str]]:
    """Redact bank / PII patterns from an outgoing answer.

    Returns ``(scrubbed_text, reasons)``; an empty ``reasons`` list means the
    text was already clean.
    """
    if not text:
        return text, []
    reasons: list[str] = []
    out = text

    def _sub(pattern: re.Pattern[str], label: str, group: int = 0) -> None:
        nonlocal out

        def _repl(m: re.Match[str]) -> str:
            if group:
                span = m.group(group)
                if label == "card-number" and not _luhn_ok(re.sub(r"\D", "", span)):
                    return m.group(0)
                reasons.append(label)
                return m.group(0).replace(span, REDACTION)
            if label == "card-number" and not _luhn_ok(re.sub(r"\D", "", m.group(0))):
                return m.group(0)
            reasons.append(label)
            return REDACTION

        out = pattern.sub(_repl, out)

    _sub(_IBAN, "iban")
    _sub(_BANK_NEAR_KEYWORD, "bank-account", group=1)
    _sub(_SSN, "ssn-or-tax-id")
    _sub(_CARD, "card-number")
    _sub(_EMAIL, "email-address")

    seen: set[str] = set()
    reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return out, reasons


def strip_sensitive_keys(record):
    """Recursively drop SENSITIVE_KEYS from a dict / list of dicts."""
    if isinstance(record, dict):
        return {k: strip_sensitive_keys(v) for k, v in record.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(record, list):
        return [strip_sensitive_keys(v) for v in record]
    return record
