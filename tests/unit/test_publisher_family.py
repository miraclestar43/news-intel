"""2026-08-12 repair reproduction: blogs.nvidia.com and developer.nvidia.com
were treated as different sources by the exact-hostname diversity cap,
letting both occupy TOP-story slots in the same digest."""

from __future__ import annotations

from news_intel.pipeline.publisher_family import publisher_family_of_url, registrable_domain


def test_nvidia_subdomains_normalize_to_one_family():
    assert registrable_domain("blogs.nvidia.com") == "nvidia.com"
    assert registrable_domain("developer.nvidia.com") == "nvidia.com"
    assert registrable_domain("nvidianews.nvidia.com") == "nvidia.com"


def test_unrelated_domains_stay_distinct():
    assert registrable_domain("techmeme.com") != registrable_domain("nvidia.com")
    assert registrable_domain("federalregister.gov") != registrable_domain("bls.gov")


def test_two_label_country_code_government_domains_stay_distinct():
    """pbc.gov.cn (People's Bank of China) and www.gov.cn (the general
    government portal) are different organizations sharing a country-code
    government suffix -- must not collapse into one family."""
    assert registrable_domain("pbc.gov.cn") != registrable_domain("www.gov.cn")


def test_www_prefix_stripped():
    assert registrable_domain("www.example.com") == registrable_domain("example.com")


def test_publisher_family_of_url_handles_missing_url():
    assert publisher_family_of_url(None) == ""
    assert publisher_family_of_url("") == ""


def test_publisher_family_of_url_extracts_from_full_article_url():
    assert publisher_family_of_url("https://developer.nvidia.com/blog/some-post") == "nvidia.com"
