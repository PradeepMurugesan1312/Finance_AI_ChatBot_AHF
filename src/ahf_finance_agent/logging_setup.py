"""Structured logging for the agent.

- One JSON object per log line (Cloud Foundry ships stdout to the logging
  stack; JSON keeps it queryable).
- A redaction filter runs over every record's rendered message and scrubs
  high-confidence PII / bank-data patterns, so an accidental
  ``logger.info("... %s", tool_result)`` can never leak an IBAN or card number
  into ``cf logs``. This is defence in depth — the response path has its own
  scrub (added in a later step); this one guards the logs specifically.
- Raw user message text is only logged when ``LOG_MESSAGE_TEXT=true``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

# High-confidence patterns only — a false positive that redacts a real invoice
# number is far less bad than leaking a bank account, but we still keep the
# patterns tight to stay useful.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4}\b")),
    ("ssn-or-tax-id", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card-number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]

_REDACTED = "[redacted]"


def redact(text: str) -> str:
    out = text
    for _label, pattern in _PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:  # never let logging break the app
            pass
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RedactionFilter())
    root.addHandler(handler)

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
