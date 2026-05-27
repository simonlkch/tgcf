"""Structured logging utilities for tgcf.

This module provides project-wide JSON logging with environment context and
helpers for canonical wide-event style log emission.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import socket
from typing import Any, Dict, Optional

from tgcf import __version__


def _base_context() -> Dict[str, Any]:
    return {
        "service": "tgcf",
        "service_version": __version__,
        "commit_hash": os.getenv("TGCF_COMMIT_HASH", "unknown"),
        "region": os.getenv("TGCF_REGION", "unknown"),
        "instance_id": os.getenv("TGCF_INSTANCE_ID") or socket.gethostname(),
    }


class JsonWideFormatter(logging.Formatter):
    """Format log records as structured JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            **_base_context(),
        }

        if isinstance(record.msg, dict):
            payload.update(record.msg)
        elif isinstance(record.msg, str):
            msg = record.msg.strip()
            if msg.startswith("{") and msg.endswith("}"):
                try:
                    decoded = json.loads(msg)
                    if isinstance(decoded, dict):
                        payload.update(decoded)
                    else:
                        payload["message"] = record.getMessage()
                except json.JSONDecodeError:
                    payload["message"] = record.getMessage()
            else:
                payload["message"] = record.getMessage()
        else:
            payload["message"] = record.getMessage()

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger once with structured JSON formatter."""

    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonWideFormatter())

    root.handlers.clear()
    root.addHandler(handler)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit a structured event via the supplied logger."""

    logger.log(level, {"event": event, **fields})
