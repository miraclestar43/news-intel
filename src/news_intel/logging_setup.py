"""Structured logging with mandatory secret redaction.

Every log line goes through a redaction processor before it is rendered,
regardless of which secret values are in scope, so a future secret never
needs a matching code change here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import structlog

_REDACT_PATTERNS = [
    re.compile(r"(?i)(bot)?\d{6,}:[A-Za-z0-9_-]{30,}"),  # Telegram bot tokens
    re.compile(r"(?i)(authorization\s*:\s*)(bearer\s+)?[A-Za-z0-9._-]{20,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)['\"]?[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)(sk-[A-Za-z0-9]{20,})"),
]

_SECRET_KEYS = {
    "telegram_bot_token",
    "llm_api_key",
    "guardian_api_key",
    "authorization",
    "token",
    "api_key",
    "secret",
}


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redaction_processor(logger, method_name, event_dict):  # noqa: ARG001
    for key in list(event_dict.keys()):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = "[REDACTED]"
            continue
        val = event_dict[key]
        if isinstance(val, str):
            event_dict[key] = redact_text(val)
    if isinstance(event_dict.get("event"), str):
        event_dict["event"] = redact_text(event_dict["event"])
    return event_dict


class _StdlibRedactionFilter(logging.Filter):
    """2026-08-29 production incident (security): the Telegram bot token
    was written to the systemd journal in PLAINTEXT on every successful
    send -- httpx logs "HTTP Request: POST
    https://api.telegram.org/bot<TOKEN>/sendMessage" at INFO level through
    the STANDARD library logger, which never passes through structlog's
    _redaction_processor. The redaction patterns were already correct;
    they were simply never applied to third-party stdlib log records.

    Attached to every root handler so any library (httpx, telegram, urllib3,
    ...) that logs a URL or header containing a credential is redacted at
    the handler boundary, rather than requiring each library to be
    individually silenced."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_text(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_text(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def configure_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(
                log_dir / "news-intel.log",
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
            )
        )

    logging.basicConfig(level=level, format="%(message)s", handlers=handlers)

    # Attach to the handlers actually installed on the root logger, not to
    # the list built above: logging.basicConfig() is a NO-OP when the root
    # logger already has handlers (another library, an embedding host, or a
    # test runner got there first), in which case the handlers above are
    # discarded and filtering them would silently protect nothing.
    redaction_filter = _StdlibRedactionFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _StdlibRedactionFilter) for f in handler.filters):
            handler.addFilter(redaction_filter)

    # Defense in depth alongside the redaction filter: httpx's INFO-level
    # request line has no diagnostic value here and is exactly where the
    # bot token leaked from.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
