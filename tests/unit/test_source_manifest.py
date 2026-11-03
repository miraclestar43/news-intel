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


def test_general_news_sources_added_2026_11_01(config):
    """news-intel v2: both editions get general China/world feeds (live-verified 2026-11-01)."""
    by_id = {s.id: s for s in config.sources.sources}
    zh = {"chinanews_china", "chinanews_world", "bbc_zhongwen", "rfi_cn", "dw_chinese"}
    en = {"guardian_world", "aljazeera_all", "france24_en", "scmp_china"}
    for sid in zh | en:
        assert by_id[sid].enabled and by_id[sid].adapter == "rss" and by_id[sid].tier == 1
    assert all(by_id[s].editions == ["zh_cn"] for s in zh)
    assert all(by_id[s].editions == ["en_global"] for s in en)


def test_a_second_edition_on_one_source_is_rejected_not_ignored():
    import pytest

    from news_intel.settings import SourcesConfig

    with pytest.raises(ValueError, match="exactly one edition"):
        SourcesConfig.model_validate(
            {"sources": [{"id": "x", "name": "x", "adapter": "rss", "tier": 1, "editions": ["en_global", "zh_cn"]}]}
        )
