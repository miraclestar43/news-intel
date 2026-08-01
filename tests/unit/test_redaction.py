from news_intel.logging_setup import redact_text
from news_intel.settings import Secrets


def test_redact_telegram_bot_token():
    text = "using token 123456789:AAHdW9k2ZZ1a2b3c4d5e6f7g8h9i0jklmno for the bot"
    assert "AAHdW9k2ZZ1a2b3c4d5e6f7g8h9i0jklmno" not in redact_text(text)
    assert "[REDACTED]" in redact_text(text)


def test_redact_bearer_authorization_header():
    text = "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789"
    redacted = redact_text(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in redacted


def test_redact_api_key_assignment():
    text = "api_key=sk-thisisaveryrealsecretlookingkey1234"
    redacted = redact_text(text)
    assert "thisisaveryrealsecretlookingkey1234" not in redacted


def test_secrets_repr_never_leaks(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAHreallysecrettokenvalue00000000000")
    monkeypatch.setenv("LLM_API_KEY", "sk-anothersecretvalue000000000000000")
    secrets = Secrets()
    rendered = repr(secrets)
    assert "AAHreallysecrettokenvalue" not in rendered
    assert "anothersecretvalue" not in rendered
    assert rendered == "Secrets(<redacted>)"
    assert str(secrets) == "Secrets(<redacted>)"
