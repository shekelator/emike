from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

# Bot tokens appear inside third-party log text (urllib3 logs full request
# URLs, which contain "bot<id>:<token>" for every Telegram call). Scrub them
# from every record that passes through the root handler.
_BOT_TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{10,}")


class _TokenScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _BOT_TOKEN_PATTERN.sub("[REDACTED]", str(record.msg))
        record.args = tuple(
            _BOT_TOKEN_PATTERN.sub("[REDACTED]", str(arg)) if isinstance(arg, str) else arg
            for arg in (record.args if isinstance(record.args, tuple) else (record.args,))
        )
        return True


class StructuredConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extras = _extract_extras(record)
        if extras:
            payload["context"] = extras

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


def _extract_extras(record: logging.LogRecord) -> dict[str, object]:
    reserved = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
    }
    out: dict[str, object] = {}
    for key, value in record.__dict__.items():
        if key not in reserved:
            out[key] = value
    return out


def configure_logging(debug_mode: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredConsoleFormatter())
    handler.addFilter(_TokenScrubFilter())

    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    # urllib3 records full request URLs at DEBUG — exactly where bot tokens
    # live. The scrub filter above is the belt; this is the suspenders: keep
    # urllib3 at INFO so its request lines (and connection churn) stay quiet
    # even in --debug runs.
    logging.getLogger("urllib3").setLevel(logging.INFO)
