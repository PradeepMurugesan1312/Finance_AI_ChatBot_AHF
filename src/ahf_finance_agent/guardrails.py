"""Output guardrails.

The design brief is explicit: no PII or vendor banking data in responses. Two
layers defend that:

1. Field-level — the S/4HANA ``$select`` lists (build step 3) never request a
   sensitive field, and :func:`strip_sensitive_keys` drops any that appear in
   a tool result anyway. Two narrow, explicitly-approved exceptions:
   ``S4HANAClient.get_vendor_email_addresses`` (email) and
   ``get_vendor_bank_accounts`` (IBAN / bank account / SWIFT — the
   payment-redirection-fraud-risk category) deliberately do not run that
   strip — see each method's docstring.
2. Text-level — :func:`scrub_response` runs over the final answer string just
   before it goes back to Joule, redacting anything that still looks like a
   card number or SSN/tax id. It does NOT redact email addresses or IBAN/bank
   account patterns (both removed 2026-09, in step with the field-level
   exceptions above — blanket-redacting either would have silently broken
   those two tools' answers).

:func:`scrub_response` is conservative about false positives — redacting a
legitimate invoice number would be worse than the rare redaction of an
already access-controlled value that slipped through.
"""

from __future__ import annotations

import re

REDACTION = "[redacted]"

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# NOTE: s4hana.S4HANAClient.get_vendor_email_addresses() (email) and
# get_vendor_bank_accounts() (IBAN / bank account / SWIFT) are two deliberate,
# narrow, explicitly-approved exceptions that do NOT run strip_sensitive_keys
# on their respective fields (see each method's docstring). Every other
# lookup in this codebase still has all of these fields blocked via this set
# — don't add further exceptions without the same kind of explicit,
# risk-accepted product sign-off.
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

    _sub(_SSN, "ssn-or-tax-id")
    _sub(_CARD, "card-number")
    # No email or IBAN/bank-account redaction here (deliberately, 2026-09):
    # get_vendor_email_addresses and get_vendor_bank_accounts are now allowed
    # to surface those fields, and blanket-redacting them in the final text
    # would silently break those two tools' answers. Field-level blocking
    # (SENSITIVE_KEYS) still applies to every other lookup in the codebase.

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
