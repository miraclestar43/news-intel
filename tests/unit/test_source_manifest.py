def test_at_least_12_sources_configured(config):
    assert len(config.sources.sources) >= 12


def test_adapter_type_diversity(config):
    adapter_types = {s.adapter for s in config.sources.sources}
    assert {"rss", "json_federal_register", "json_hn", "ical", "gdelt", "html_index", "social_stub"} <= adapter_types


def test_social_handles_disabled_by_default(config):
    social = [s for s in config.sources.sources if s.adapter == "social_stub"]
    assert len(social) == 3
    assert all(not s.enabled for s in social)


def test_chinese_sources_present(config):
    chinese_sources = [s for s in config.sources.sources if "china" in s.category_hints]
    assert len(chinese_sources) >= 2


def test_at_least_one_enabled_chinese_t0_source(config):
    """Phase 1 hardening requirement: at least one Chinese official T0
    source must be configured enabled (not merely declared). pboc_policy is
    intentionally disabled (pbc.gov.cn/robots.txt disallows non-Baidu
    crawlers); gov_cn_policy carries this requirement."""
    enabled_chinese_t0 = [
        s
        for s in config.sources.sources
        if "china" in s.category_hints and s.tier == 0 and s.enabled
    ]
    assert len(enabled_chinese_t0) >= 1
