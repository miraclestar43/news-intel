"""2026-10-31 news-quality branch (history-in-practice 3-minute news audit): provider outcomes are recorded for
every daily run, and a flagged public-safety bucket keeps a major epidemic/disaster from being Unclassified."""

from __future__ import annotations

import json

from news_intel.pipeline.relevance import PUBLIC_SAFETY_BUCKET, UNCLASSIFIED_BUCKET, classify_relevance
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.settings import load_config
from tests.unit.test_daily_production import _NOW, _seed_event

CONFIG = load_config()
EBOLA = "More than 5,000 die from Ebola in DR Congo's deadliest outbreak"
EBOLA_EXCERPT = "Government figures show more than 10,000 people have been infected."
CHARGER = "129 元，酷态科 65W 氮化镓充电器上架"


def _classify(text, *, flag, topics=None, title=None):
    return classify_relevance(
        text, topics or CONFIG.topics, CONFIG.policy.relevance_multipliers, title=title, public_safety=flag
    )


def test_flag_defaults_off_in_the_shipped_policy():
    assert CONFIG.policy.features.public_safety_bucket is False


def test_ebola_outbreak_is_unclassified_without_the_flag_and_public_safety_with_it():
    assert _classify(f"{EBOLA} {EBOLA_EXCERPT}", flag=False, title=EBOLA).bucket == UNCLASSIFIED_BUCKET
    m = _classify(f"{EBOLA} {EBOLA_EXCERPT}", flag=True, title=EBOLA)
    assert m.bucket == PUBLIC_SAFETY_BUCKET and m.multiplier > 1.0
    assert "ebola" in m.matched_terms


def test_public_safety_needs_scale_not_just_an_event_word():
    assert (
        _classify("A minor earthquake shook the town overnight; no injuries were reported", flag=True).bucket
        != PUBLIC_SAFETY_BUCKET
    )
    assert _classify("Three people died in a crash on the motorway", flag=True).bucket != PUBLIC_SAFETY_BUCKET
    assert (
        _classify("Floods kill 40 as monsoon rains hit the region; 40 people killed", flag=True).bucket
        == PUBLIC_SAFETY_BUCKET
    )


def test_chinese_public_safety_and_a_gadget_listing():
    zh = CONFIG.topics_for("zh_cn")
    assert _classify("某化工厂发生爆炸，造成12人死亡", flag=True, topics=zh).bucket == PUBLIC_SAFETY_BUCKET
    assert _classify(CHARGER, flag=True, topics=zh).bucket != PUBLIC_SAFETY_BUCKET
    game = "《使命召唤》游戏使用梨泰院踩踏事故素材，动视致歉"  # replay false positive: media about a past disaster
    assert _classify(f"{game} 事故造成159人死亡", flag=True, topics=zh, title=game).bucket != PUBLIC_SAFETY_BUCKET


def test_a_major_epidemic_qualifies_for_the_daily_only_with_the_flag(config, conn):
    _seed_event(conn, config, event_id="E-EBOLA", title=EBOLA, category="x", excerpt=EBOLA_EXCERPT)
    _seed_event(
        conn,
        config,
        event_id="E-PHONE",
        title="New phone launches with a bigger battery and faster AI chip",
        category="x",
        excerpt="The phone uses a new AI chip.",
    )
    weights = config.policy.scoring.weights.model_dump()

    def rescore():
        for eid in ("E-EBOLA", "E-PHONE"):
            persist_score(
                conn,
                eid,
                score_event(conn, eid, config, is_new_event=True, now=_NOW),
                weights,
                config.policy.scoring.thresholds,
            )
        return {
            r["id"]: (r["category"], r["final_score"])
            for r in conn.execute("SELECT id, category, final_score FROM events")
        }

    bar = config.policy.scoring.thresholds.daily_quick_brief
    off = rescore()
    assert off["E-EBOLA"][0] == "Unclassified" and off["E-EBOLA"][1] < bar  # cannot even be a quick brief
    config.policy.features.public_safety_bucket = True
    on = rescore()
    assert on["E-EBOLA"][0] == "Public Safety" and on["E-EBOLA"][1] >= bar
    assert on["E-PHONE"] == off["E-PHONE"]  # other stories untouched


def test_daily_run_records_why_it_fell_back(config, conn, capsys):
    from news_intel.jobs.daily import run_daily

    _seed_event(
        conn, config, event_id="E-1", title="Chipmaker unveils new AI accelerator", category="AI & Semiconductors"
    )
    run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW)
    row = conn.execute("SELECT detail_json FROM job_runs WHERE job_name='daily' ORDER BY id DESC LIMIT 1").fetchone()
    detail = json.loads(row["detail_json"])
    out = detail["provider_outcome"]
    assert detail["fallback_used"] is True and out["digest_call"].startswith("unavailable")
    assert out["fallback_reason"].startswith("no story-level provider configured")
    assert "provider_outcome=" in capsys.readouterr().out


def test_chinese_amounts_with_a_space_or_another_currency_are_traceable():
    # real IT之家 formatting (2026-10-31 zh_cn diagnosis): every such figure made the model's correct sentence fail
    from news_intel.pipeline.editorial_validation import _numbers_in

    assert 32e12 in _numbers_in("营业利润合计 达到 32 万亿日元 （IT之家注：现汇率约合 1.35 万亿元人民币）")
    assert 32e12 in _numbers_in("营业利润合计达到32万亿日元")
    assert 1.07e14 in _numbers_in("三星电子上季度营业利润 达到约 107 万亿韩元")
    assert 63e8 in _numbers_in("约 63 亿美元") and 63.0 not in _numbers_in("约 63 亿美元")
