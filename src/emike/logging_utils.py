from __future__ import annotations

import json
import logging
from datetime import UTC, datetime


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

    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug_mode else logging.INFO)
