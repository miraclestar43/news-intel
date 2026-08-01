from news_intel.settings import Secrets


def test_allowed_chat_id_list_parses_csv(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111, 222,333")
    secrets = Secrets()
    assert secrets.allowed_chat_id_list == [111, 222, 333]


def test_allowed_chat_id_list_empty_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    secrets = Secrets()
    assert secrets.allowed_chat_id_list == []


def test_policy_yaml_and_sources_yaml_load_without_error(config):
    assert config.policy.timezone == "America/Los_Angeles"
    assert config.policy.quiet_hours.start == "23:30"
    assert config.policy.quiet_hours.end == "06:30"
    assert config.policy.alerts.s2_daily_budget == 3
