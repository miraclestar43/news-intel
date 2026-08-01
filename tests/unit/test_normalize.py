from news_intel.pipeline.normalize import canonicalize_url, content_hash, normalize_title, url_hash


def test_canonicalize_strips_tracking_params():
    a = canonicalize_url("https://Example.com/story/?utm_source=x&id=42&fbclid=abc")
    b = canonicalize_url("https://example.com/story?id=42")
    assert a == b


def test_canonicalize_normalizes_trailing_slash_and_case():
    a = canonicalize_url("https://Example.COM/Story/")
    b = canonicalize_url("https://example.com/Story")
    assert a == b


def test_url_hash_stable():
    u = canonicalize_url("https://example.com/a?b=1")
    assert url_hash(u) == url_hash(u)


def test_normalize_title_strips_punctuation_and_case():
    assert normalize_title("NVIDIA's New Chip — Launched!") == "nvidia s new chip launched"


def test_content_hash_depends_on_excerpt():
    h1 = content_hash("same title", "excerpt one")
    h2 = content_hash("same title", "excerpt two")
    assert h1 != h2
