"""Structured JSON logging with secret redaction.

Every log line is a single JSON object with ``ts``, ``level``, ``logger``,
``event`` and any structured fields passed via ``extra={"fields": {...}}`` or the
``log_event`` helper. A redaction filter removes PEM blocks, bearer tokens and
Kalshi signature headers from messages and fields, so a private key can never
reach a log sink even if code accidentally passes it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_PEM_PARTIAL = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.S)
_SENSITIVE_KEYS = {"kalshi-access-signature", "kalshi-access-key", "authorization",
                   "private_key", "private_key_pem", "api_key", "x-api-key", "anthropic_api_key",
                   "signature", "secret", "password", "token"}
_BEARER = re.compile(r"(?i)(bearer|sk-ant-[a-z0-9]*)[-_a-z0-9.]{8,}")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        v = _PEM.sub("[REDACTED PRIVATE KEY]", value)
        v = _PEM_PARTIAL.sub("[REDACTED PRIVATE KEY]", v)
        return _BEARER.sub("[REDACTED TOKEN]", v)
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else redact(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": round(record.created, 6),
            "level": record.levelname,
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(redact(fields))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event. Categories used across the codebase:
    signal, order, cancel, fill, error, risk, kill_switch, ws, reconnect."""
    logger.log(level, event, extra={"fields": fields})


def now_ns() -> int:
    return time.time_ns()
