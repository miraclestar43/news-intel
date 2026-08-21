"""zh_cn source-quality requirements not covered elsewhere:
- Chinese noise (entertainment/celebrity/sports/lifestyle) rejection,
  using the same shared classify_relevance()/noise_exclusions mechanism
  as English, config-driven via topics_zh.yaml.
- Explicit source attribution (官方表示/新华社报道称/援引知情人士) is
  preserved verbatim through the render pipeline, never converted into an
  unqualified fact.
- A single official/state-media source is not, by itself, treated as
  independently corroborated -- the shared confirmation-state mechanism
  (edition-agnostic) requires cross-family corroboration the same way it
  already does for English T0 sources."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.pipeline.relevance import UNCLASSIFIED_BUCKET, classify_relevance
from news_intel.pipeline.zh_render_readiness import what_happened_zh
from news_intel.settings import load_config

CONFIG = load_config()
ZH_TOPICS = CONFIG.topics_for("zh_cn")
MULTIPLIERS = CONFIG.policy.relevance_multipliers


def _classify_zh(text: str):
    return classify_relevance(text, ZH_TOPICS, MULTIPLIERS)


# --- Chinese noise rejection --------------------------------------------


def test_celebrity_gossip_is_rejected_as_noise():
    result = _classify_zh("某明星与网红传闻恋情曝光，八卦话题引发热议")
    assert result.bucket == "explicit_noise"


def test_sports_coverage_is_rejected_as_noise():
    result = _classify_zh("昨晚球赛结果揭晓，体育迷彻夜关注比分")
    assert result.bucket == "explicit_noise"


def test_product_discount_promotion_is_rejected_as_noise():
    result = _classify_zh("双十一优惠促销活动开启，全场折扣力度空前")
    assert result.bucket == "explicit_noise"


def test_genuine_policy_story_is_not_rejected_as_noise():
    result = _classify_zh("国家发展改革委发布人工智能产业发展指导意见，加大对先进制程芯片的政策支持")
    assert result.bucket != "explicit_noise"
    assert result.bucket != UNCLASSIFIED_BUCKET


def test_lifestyle_content_with_incidental_tech_word_still_rejected():
    """General event-type/consequence gating applies the same way it does
    to English -- a bare vocabulary hit inside otherwise low-value content
    doesn't rescue it; entertainment content mentioning a gadget in
    passing is still noise, not a tech story."""
    result = _classify_zh("网红在直播中晒出新款手机，粉丝热议其穿搭，娱乐话题持续发酵")
    assert result.bucket == "explicit_noise"


def test_bare_five_year_plan_mention_alone_does_not_classify_as_ai_semiconductor():
    """Real misclassification found via a live collection run: a State
    Council notice about a LEGAL-EDUCATION five-year plan was classified
    into the AI/semiconductor bucket purely because it mentioned "五年规划"
    -- China's five-year planning cycle covers every sector, not just tech
    policy, so a bare mention alone must never qualify."""
    text = "中共中央 国务院转发《中央宣传部、司法部关于开展法治宣传教育的第九个五年规划（2026-2030年）》"
    result = _classify_zh(text)
    assert result.bucket != "ai_semiconductor_export_controls"


def test_bare_state_council_letterhead_mention_alone_does_not_classify_as_china_policy():
    """Same defect, second cause: "国务院"/"中共中央" are the issuing-
    authority letterhead on nearly every Chinese government document
    regardless of subject -- a bare mention carries no china-policy
    substance signal on its own."""
    result = _classify_zh("国务院办公厅关于印发城市绿化管理若干规定的通知")
    assert result.bucket != "us_china_policy_geopolitics"


def test_genuine_ai_policy_story_still_classifies_correctly_after_the_fix():
    """Non-regression: removing the over-generic terms must not suppress
    genuinely material AI/semiconductor policy content."""
    result = _classify_zh("国家发展改革委发布人工智能产业发展指导意见，明确对先进制程芯片的政策支持")
    assert result.bucket == "ai_semiconductor_export_controls"


def test_genuine_china_us_relations_story_still_classifies_correctly_after_the_fix():
    result = _classify_zh("外交部发言人就中美关系发表最新表态")
    assert result.bucket == "us_china_policy_geopolitics"


def test_genuine_export_control_story_still_classifies_correctly_after_the_fix():
    result = _classify_zh("商务部宣布对相关企业实施新一轮出口管制措施")
    assert result.bucket == "ai_semiconductor_export_controls"


# --- attribution preservation --------------------------------------------


def test_official_spokesperson_attribution_is_preserved_verbatim():
    primary = {
        "title": "商务部新闻发言人表示",
        "excerpt": "商务部新闻发言人表示，中方对相关贸易措施保持高度关注。",
    }
    result = what_happened_zh(primary)
    assert result == "商务部新闻发言人表示，中方对相关贸易措施保持高度关注。"
    assert "表示" in result  # explicit attribution verb retained, not stripped


def test_wire_service_attribution_is_preserved_verbatim():
    primary = {"title": "标题", "excerpt": "新华社报道称，相关部门正在评估具体影响。"}
    result = what_happened_zh(primary)
    assert result == "新华社报道称，相关部门正在评估具体影响。"
    assert "新华社报道称" in result


def test_unconfirmed_claim_attribution_is_preserved_not_upgraded_to_fact():
    primary = {"title": "标题", "excerpt": "据财新援引知情人士消息，该交易尚未获得最终批准。该说法尚未得到独立证实。"}
    result = what_happened_zh(primary)
    assert result is not None
    assert "据财新援引知情人士" in result
    assert "尚未得到独立证实" in result


# --- official source is not, by itself, independent corroboration -------


def test_single_official_source_confirmation_state_is_not_auto_confirmed(config, conn):
    """Mirrors the EXISTING, edition-agnostic confirmation-state mechanism
    (pipeline/confirmation.py) -- a single T0/official-sourced event never
    starts at a "confirmed by independent corroboration" state merely
    because the source is authoritative. Government/state-media assertion
    establishes what was ANNOUNCED, not independent confirmation of a
    disputed claim -- the same rule already applied to English T0 sources
    now applies identically to a zh_cn official source, with zero
    edition-specific code."""
    from news_intel.pipeline.confirmation import update_confirmation_state
    from tests.conftest import insert_source_row

    now = datetime(2026, 8, 18, 7, 0, tzinfo=UTC)
    insert_source_row(conn, "src_mfa_zh", 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state, edition_id) VALUES ('E-ZH-OFFICIAL', '外交部发言人表示', ?, ?, ?, "
        "'unverified', 'zh_cn')",
        (now.isoformat(), now.isoformat(), now.isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family, edition_id) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 'zh', ?, ?, ?, 'zh_cn')",
        ("src_mfa_zh", "e1", "https://mfa.gov.cn/x", "h1", "外交部发言人表示", "外交部发言人表示",
         "外交部发言人就相关问题表态。", now.isoformat(), "c1", "mfa.gov.cn"),
    )
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES "
        "('E-ZH-OFFICIAL', ?, 1, '[]')",
        (cur.lastrowid,),
    )
    update_confirmation_state(conn, "E-ZH-OFFICIAL", now=now)

    row = conn.execute("SELECT confirmation_state FROM events WHERE id = 'E-ZH-OFFICIAL'").fetchone()
    # A single source (even an authoritative T0 one) must not reach the
    # fully-independently-corroborated state -- exact state name depends
    # on pipeline/confirmation.py's own vocabulary, but "confirmed" (an
    # explicit cross-family corroboration claim) must not be it.
    assert row["confirmation_state"] != "confirmed"


# 2026-08-21: real production incident fixture (Aug 19 zh_cn digest,
# already delivered) -- a 100% domestic battery-supply cooperation story
# between two Chinese companies (BYD's 弗迪 unit and FAW/一汽) classified
# as "US / China / World" purely because the text said "中国一汽" (FAW's
# own Chinese name literally contains "中国").
def test_byd_faw_domestic_battery_story_is_not_us_china_world():
    text = (
        "比亚迪二代刀片电池、兆瓦闪充首次对外开放，一汽红旗下一代纯电平台率先获定点 "
        "一汽弗迪新能源动力电池项目由中国一汽与比亚迪共同打造，正在推进弗迪二代刀片电池的导入适配，"
        "即将实现兆瓦闪充技术在中国一汽车型上的首次搭载。"
    )
    result = _classify_zh(text)
    assert result.bucket != "us_china_policy_geopolitics"
