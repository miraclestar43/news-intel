def test_at_least_12_sources_configured(config):
    assert len(config.sources.sources) >= 12


def test_adapter_type_diversity(config):
    adapter_types = {s.adapter for s in config.sources.sources}
    assert {"rss", "json_federal_register", "json_hn", "ical", "gdelt", "html_index", "social_stub"} <= adapter_types


def test_social_handles_disabled_by_default(config):
    social = [s for s in config.sources.sources if s.adapter == "social_stub"]
    assert len(social) == 4  # includes 2026-08-13's social_amitisinvesting evaluation
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


def test_bbc_world_and_china_sources_present_and_enabled(config):
    """2026-08-13: closes the structural Politics/World and China/US-China
    source gap confirmed across rounds 2-3."""
    by_id = {s.id: s for s in config.sources.sources}
    assert by_id["bbc_world"].enabled is True
    assert by_id["bbc_world"].adapter == "rss"
    assert by_id["bbc_china"].enabled is True
    assert by_id["bbc_china"].adapter == "rss"
