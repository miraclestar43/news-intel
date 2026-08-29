"""2026-08-29 production incident (security): the Telegram bot token was
written to the systemd journal in plaintext on every successful send. The
leaking line came from httpx at INFO level:

    HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage "HTTP/1.1 200 OK"

structlog's redaction processor never saw it, because httpx logs through
the standard library. The redaction PATTERNS were already correct -- they
were simply never applied to stdlib records."""

from __future__ import annotations

import logging

from news_intel.logging_setup import _StdlibRedactionFilter, configure_logging, redact_text

# Shape-accurate, non-real credential.
_FAKE_TOKEN = "1111111111:AAFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"


def test_redact_text_already_handled_this_shape():
    assert _FAKE_TOKEN not in redact_text(f"POST https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage")


def test_stdlib_filter_redacts_the_exact_httpx_request_line():
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f'HTTP Request: POST https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage "HTTP/1.1 200 OK"',
        args=(), exc_info=None,
    )
    _StdlibRedactionFilter().filter(record)
    assert _FAKE_TOKEN not in record.getMessage()
    assert "REDACTED" in record.getMessage()


def test_stdlib_filter_redacts_tokens_passed_as_args():
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="HTTP Request: %s", args=(f"https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage",),
        exc_info=None,
    )
    _StdlibRedactionFilter().filter(record)
    assert _FAKE_TOKEN not in record.getMessage()


def test_configure_logging_attaches_the_filter_to_every_root_handler(tmp_path):
    """Covers the case that actually bit: logging.basicConfig() is a no-op
    when the root logger already has handlers (pytest, an embedding host,
    another library), so the filter must be attached to the handlers really
    installed on root -- otherwise it protects a discarded list."""
    configure_logging(log_dir=tmp_path / "logs")
    root_handlers = logging.getLogger().handlers
    assert root_handlers, "no root handlers at all"
    for handler in root_handlers:
        assert any(isinstance(f, _StdlibRedactionFilter) for f in handler.filters), (
            f"handler {handler!r} would emit un-redacted third-party log records"
        )


def test_configure_logging_is_idempotent_and_does_not_stack_filters(tmp_path):
    configure_logging(log_dir=tmp_path / "logs")
    configure_logging(log_dir=tmp_path / "logs")
    for handler in logging.getLogger().handlers:
        count = sum(1 for f in handler.filters if isinstance(f, _StdlibRedactionFilter))
        assert count <= 1


def test_configure_logging_quiets_httpx_request_logging(tmp_path):
    configure_logging(log_dir=tmp_path / "logs")
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_ordinary_log_lines_are_untouched():
    record = logging.LogRecord(
        name="news_intel", level=logging.INFO, pathname=__file__, lineno=1,
        msg="edition=zh_cn fail_closed=False sent=True top3=2", args=(), exc_info=None,
    )
    _StdlibRedactionFilter().filter(record)
    assert record.getMessage() == "edition=zh_cn fail_closed=False sent=True top3=2"
