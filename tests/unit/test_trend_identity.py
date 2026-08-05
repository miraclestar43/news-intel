"""Gate A remediation Phase 4: weekly trend identity. Gate A produced 915
nearly-meaningless "trends" for one week, some merging unrelated headlines,
because the old algorithm let any shared entity (however generic) link two
events, and let group membership grow by transitive union rather than
complete-link validation. These tests reproduce the exact failure classes
that produced that defect and prove the fix."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.trends import TrendCluster, build_trend_clusters, select_top_clusters
from news_intel.settings import TrendIdentityPolicy
from tests.conftest import insert_source_row

_POLICY = TrendIdentityPolicy(
    generic_anchor_terms=["AI", "Market", "Markets", "Government", "Report", "Research", "United States", "US"],
    topic_trigram_floor=0.12,
)


def _seed(
    conn, *, event_id, title, category, score, entities, confq=70.0, archive_only=0, now=None,
    origin_family=None,
):
    now = now or datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    ts = now.isoformat()
    conn.execute(
        "INSERT INTO events (id, primary_title, category, status, first_seen_at, last_updated_at, "
        "published_anchor_at, archive_only, confirmation_state, final_score, confirmation_quality, "
        "entities_json) VALUES (?, ?, ?, 'developing', ?, ?, ?, ?, 'official', ?, ?, ?)",
        (event_id, title, category, ts, ts, ts, archive_only, score, confq, json.dumps(entities)),
    )
    origin_family = origin_family or f"src-{event_id}"
    insert_source_row(conn, f"src_{event_id}", 1)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (f"src_{event_id}", event_id, f"https://x.test/{event_id}", event_id, title, title.lower(),
         "excerpt", ts, event_id, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def test_generic_shared_term_alone_cannot_form_a_trend(conn):
    """Two unrelated LLM research posts that only share the generic entity
    "AI" must remain separate standalone developments, not one trend."""
    a = _seed(
        conn, event_id="E-A", title="Startup publishes new benchmark results for coding agents",
        category="AI & Semiconductors", score=80, entities=["AI"],
    )
    b = _seed(
        conn, event_id="E-B", title="University lab releases dataset for robotics perception research",
        category="AI & Semiconductors", score=78, entities=["AI"],
    )
    clusters = build_trend_clusters(conn, [a, b], known_entities=set(), policy=_POLICY)

    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)
    assert all(c.shared_anchor == [] for c in clusters)


def test_specific_entity_with_topical_overlap_forms_a_trend(conn):
    a = _seed(
        conn, event_id="E-A", title="NVIDIA export control compliance plan tightened for chip shipments",
        category="AI & Semiconductors", score=85, entities=["NVIDIA"],
    )
    b = _seed(
        conn, event_id="E-B", title="NVIDIA export control compliance plan expanded to more chip shipments",
        category="AI & Semiconductors", score=82, entities=["NVIDIA"],
    )
    clusters = build_trend_clusters(conn, [a, b], known_entities=set(), policy=_POLICY)

    assert len(clusters) == 1
    trend = clusters[0]
    assert trend.is_trend is True
    assert trend.shared_anchor == ["NVIDIA"]
    assert set(trend.event_ids) == {"E-A", "E-B"}


def test_shared_specific_entity_without_topical_similarity_stays_separate(conn):
    """Sharing a company name alone is not enough -- an export-control story
    and an unrelated earnings release about the same company are different
    developments."""
    a = _seed(
        conn, event_id="E-A", title="NVIDIA export control compliance plan tightened for chip shipments",
        category="AI & Semiconductors", score=85, entities=["NVIDIA"],
    )
    b = _seed(
        conn, event_id="E-B", title="NVIDIA reports record quarterly revenue beating analyst expectations",
        category="AI & Semiconductors", score=82, entities=["NVIDIA"],
    )
    clusters = build_trend_clusters(conn, [a, b], known_entities=set(), policy=_POLICY)

    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_transitive_bridging_is_blocked_by_complete_link_validation(conn):
    """A shares an anchor with B; B shares a *different* anchor with C; A and
    C share nothing. The old transitive-union algorithm would have merged
    all three into one blob via B as a bridge -- complete-link validation
    must keep C out since C is incompatible with A."""
    a = _seed(
        conn, event_id="E-A", score=90, category="AI & Semiconductors", entities=["NVIDIA"],
        title="NVIDIA unveils new export control compliance plan for chip shipments",
    )
    b = _seed(
        conn, event_id="E-B", score=85, category="AI & Semiconductors", entities=["NVIDIA", "TSMC"],
        title="NVIDIA export control compliance plan folds in TSMC foundry shipments",
    )
    c = _seed(
        conn, event_id="E-C", score=80, category="AI & Semiconductors", entities=["TSMC"],
        title="TSMC foundry shipments surge under new export control compliance rules",
    )
    clusters = build_trend_clusters(conn, [a, b, c], known_entities=set(), policy=_POLICY)

    trend = next(c for c in clusters if c.is_trend)
    standalones = [c for c in clusters if not c.is_trend]

    assert set(trend.event_ids) == {"E-A", "E-B"}
    assert trend.shared_anchor == ["NVIDIA"]
    assert len(standalones) == 1
    assert standalones[0].event_ids == ["E-C"]


def test_archive_only_events_are_excluded_from_trend_candidacy(conn):
    a = _seed(
        conn, event_id="E-A", score=95, category="AI & Semiconductors", entities=["NVIDIA"],
        title="NVIDIA export control compliance plan tightened for chip shipments", archive_only=1,
    )
    clusters = build_trend_clusters(conn, [a], known_entities=set(), policy=_POLICY, min_event_score=0.0)
    assert clusters == []


def test_below_threshold_events_are_excluded_from_trend_candidacy(conn):
    a = _seed(
        conn, event_id="E-A", score=10, category="AI & Semiconductors", entities=["NVIDIA"],
        title="Minor NVIDIA blog post about a UI tweak",
    )
    clusters = build_trend_clusters(conn, [a], known_entities=set(), policy=_POLICY, min_event_score=45.0)
    assert clusters == []


def test_unclassified_and_noise_categories_never_qualify_as_trends_or_standalones(conn):
    """Reproduces the Gate A NAGPRA (honestly unclassified) and a
    boilerplate-noise example remaining separate and out of the weekly
    trend output entirely, regardless of score."""
    nagpra = _seed(
        conn, event_id="E-NAGPRA", score=70, category="Unclassified", entities=[],
        title="Notice of Inventory Completion under NAGPRA",
    )
    noise = _seed(
        conn, event_id="E-NOISE", score=60, category="noise", entities=[],
        title="Law firm shareholder alert boilerplate notice",
    )
    clusters = build_trend_clusters(
        conn, [nagpra, noise], known_entities=set(), policy=_POLICY, min_event_score=0.0
    )
    assert clusters == []


def test_select_top_clusters_bounds_to_configured_max():
    clusters = [
        TrendCluster(id=f"TR-{i}", category="cat", key_entities=[], event_ids=[f"E-{i}"], strength=100 - i)
        for i in range(20)
    ]
    selected = select_top_clusters(clusters, max_items=5)
    assert len(selected) == 5
    assert [c.id for c in selected] == [f"TR-{i}" for i in range(5)]  # highest-strength first


def test_gate_a_retry1_conflicting_gpt_versions_do_not_form_one_weekly_trend(conn):
    """Regression fixture reproducing the exact Retry 1 finding: a weekly
    trend that grouped "Introducing GPT-5.5" with a differently-versioned
    Codex model under a shared "OpenAI" anchor. A slash-separated list of
    different products/versions is not a shared anchor -- an explicit
    version conflict must keep them out of the same trend even though they
    share a specific (non-generic) entity and would otherwise clear the
    topical-trigram floor."""
    a = _seed(
        conn, event_id="E-A", score=85, category="AI & Semiconductors", entities=["OpenAI"],
        title="Introducing GPT-5.5",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="AI & Semiconductors", entities=["OpenAI"],
        title="Introducing GPT-5.3-Codex-Spark",
    )
    clusters = build_trend_clusters(conn, [a, b], known_entities=set(), policy=_POLICY)

    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_gate_a_retry1_organization_name_alone_does_not_anchor_unrelated_launches(config, conn):
    """Regression fixture reproducing a deeper Retry 1 re-verification
    finding: replaying real production data showed "OpenAI" alone
    anchoring a 17-member trend mixing office openings, model launches,
    and corporate restructuring. Using the *real* config (where OpenAI,
    NVIDIA, etc. are in trend_identity.generic_anchor_terms), two
    completely unrelated OpenAI announcements sharing only the
    organization name must not form one trend."""
    a = _seed(
        conn, event_id="E-A", score=85, category="AI & Semiconductors", entities=["OpenAI"],
        title="Evolving OpenAI's structure",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="AI & Semiconductors", entities=["OpenAI"],
        title="Introducing OpenAI for Australia",
    )
    known = known_entities(config.topics.groups)
    clusters = build_trend_clusters(
        conn, [a, b], known, policy=config.topics.trend_identity,
        min_event_score=config.policy.scoring.thresholds.weekly_store,
    )

    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_gate_a_retry1_generic_platform_terms_do_not_anchor_unrelated_posts(config, conn):
    """Regression fixture reproducing further Retry 1 re-verification
    findings on real production data: "GPU" and "NVIDIA CUDA" alone
    anchored pairs of completely unrelated posts (a distributed-SQL
    workload post vs. a medical-physics simulation post; a kernel-fusion
    optimization post vs. a cryptography post) using the *real* config."""
    known = known_entities(config.topics.groups)
    policy = config.topics.trend_identity
    min_score = config.policy.scoring.thresholds.weekly_store

    a = _seed(
        conn, event_id="E-A", score=85, category="AI & Semiconductors", entities=["GPU", "NVIDIA"],
        title="Running Low-Latency Analytical Workloads with GPU-Accelerated Presto on NVIDIA GB200",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="AI & Semiconductors", entities=["GPU", "NVIDIA"],
        title="NVIDIA Open Sources First GPU-Accelerated Medical Physics Simulation Framework",
    )
    clusters = build_trend_clusters(conn, [a, b], known, policy=policy, min_event_score=min_score)
    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_gate_a_retry1_federal_reserve_board_alone_does_not_anchor_distinct_actions(config, conn):
    """14 genuinely distinct Fed regulatory actions (bank approvals,
    unrelated enforcement actions against different banks) were merged
    into one "trend" on "Federal Reserve Board" alone."""
    known = known_entities(config.topics.groups)
    policy = config.topics.trend_identity
    min_score = config.policy.scoring.thresholds.weekly_store

    a = _seed(
        conn, event_id="E-A", score=85, category="Markets & Economy", entities=["Federal Reserve Board"],
        title="Federal Reserve Board announces approval of the application by Banco Santander, S.A.",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="Markets & Economy", entities=["Federal Reserve Board"],
        title="Federal Reserve Board issues enforcement action with Iuka Bancshares, Inc.",
    )
    clusters = build_trend_clusters(conn, [a, b], known, policy=policy, min_event_score=min_score)
    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_gate_a_retry1_bare_gpt_does_not_bridge_unrelated_posts_but_same_version_still_clusters(config, conn):
    """Bare "GPT" (no explicit version on one/either side) must not bridge
    unrelated posts, but two posts that both explicitly claim the *same*
    GPT version must still form a trend even though "GPT" itself is now a
    blacklisted generic anchor term -- via the independent matching-
    version path in trends.py's _compatible()."""
    known = known_entities(config.topics.groups)
    policy = config.topics.trend_identity
    min_score = config.policy.scoring.thresholds.weekly_store

    unrelated_a = _seed(
        conn, event_id="E-UA", score=85, category="AI & Semiconductors", entities=["OpenAI"],
        title="WebGPT: Improving the factual accuracy of language models through web browsing",
    )
    unrelated_b = _seed(
        conn, event_id="E-UB", score=82, category="AI & Semiconductors", entities=["OpenAI"],
        title="Preserving languages for the future",
    )
    same_version_a = _seed(
        conn, event_id="E-SA", score=80, category="AI & Semiconductors", entities=["OpenAI"],
        title="GPT-5.6: Frontier intelligence that scales with your ambition",
    )
    same_version_b = _seed(
        conn, event_id="E-SB", score=78, category="AI & Semiconductors", entities=["OpenAI"],
        title="How GPT-5.6 fuses frontier intelligence with frontier efficiency",
    )
    clusters = build_trend_clusters(
        conn, [unrelated_a, unrelated_b, same_version_a, same_version_b],
        known, policy=policy, min_event_score=min_score,
    )
    trends = [c for c in clusters if c.is_trend]
    assert len(trends) == 1
    assert set(trends[0].event_ids) == {"E-SA", "E-SB"}


def test_gate_a_retry1_system_card_template_does_not_bridge_different_models(config, conn):
    """"System Card" is OpenAI's recurring document-type label for any
    model -- GPT-4o and GPT-5 system cards must not merge just because
    they share that template phrase."""
    known = known_entities(config.topics.groups)
    policy = config.topics.trend_identity
    min_score = config.policy.scoring.thresholds.weekly_store

    a = _seed(
        conn, event_id="E-A", score=85, category="AI & Semiconductors", entities=["OpenAI"],
        title="Addendum to GPT-4o System Card: 4o image generation",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="AI & Semiconductors", entities=["OpenAI"],
        title="Addendum to GPT-5 System Card: Sensitive conversations",
    )
    clusters = build_trend_clusters(conn, [a, b], known, policy=policy, min_event_score=min_score)
    assert len(clusters) == 2
    assert all(not c.is_trend for c in clusters)


def test_diagnostic_shared_anchor_and_event_ids_present_for_every_trend(conn):
    a = _seed(
        conn, event_id="E-A", score=85, category="Markets & Economy", entities=["Federal Reserve"],
        title="Federal Reserve signals rate path shift amid inflation data",
    )
    b = _seed(
        conn, event_id="E-B", score=82, category="Markets & Economy", entities=["Federal Reserve"],
        title="Federal Reserve rate path shift confirmed in follow-up remarks",
    )
    clusters = build_trend_clusters(conn, [a, b], known_entities=set(), policy=_POLICY)
    trend = clusters[0]
    assert trend.is_trend is True
    assert trend.shared_anchor  # non-empty diagnostic explanation
    assert set(trend.event_ids) == {"E-A", "E-B"}
