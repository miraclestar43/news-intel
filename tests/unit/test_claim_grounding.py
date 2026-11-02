"""2026-11-01 (news-intel v2): claim-level grounding in pipeline/editorial_validation.py, on the real failure shapes:
the Ebola headline-only death toll, the 国铁集团 English gloss, IT之家 万亿日元 amounts, figures that exist somewhere in
the evidence but not for the claim, and comments on what the sources lack."""

from __future__ import annotations

import time

import pytest

from news_intel.llm.errors import LLMUnavailableError
from news_intel.models import EventEnrichment
from news_intel.pipeline import editorial_enrichment
from news_intel.pipeline.editorial_evidence import build_event_input
from news_intel.pipeline.editorial_validation import validate_enrichment

EBOLA = {
    "event_id": "E-ebola", "title": "More than 5,000 die from Ebola in DR Congo's deadliest outbreak",
    "excerpt": "Government data shows that more than 10,000 people have contracted the rare virus.",
    "entities": [],
    "sources": [{"publisher": "BBC News",
                 "raw_title": "More than 5,000 die from Ebola in DR Congo's deadliest outbreak",
                 "excerpt": "Government data shows that more than 10,000 people have contracted the rare virus.",
                 "url": "https://www.bbc.co.uk/news/articles/x", "published_at": "2026-10-31T09:12:00+00:00",
                 "origin_family": "bbc.co.uk"}],
}
RAIL = {
    "event_id": "E-rail", "title": "国铁集团：2026 年前三季度营收 9143 亿元，净利润盈利 130 亿元",
    "excerpt": "IT之家 10 月 31 日消息，10 月 30 日，国铁集团披露了 2026 年前三季度财务决算。"
               "前三季度，国铁集团实现营业总收入 9143 亿元，"
               "净利润盈利 130 亿元，多项运输指标创历史同期新高。",
    "entities": ["国铁集团"],
    "sources": [{"publisher": "IT之家", "raw_title": "国铁集团：2026 年前三季度营收 9143 亿元，净利润盈利 130 亿元",
                 "excerpt": "IT之家 10 月 31 日消息，10 月 30 日，国铁集团披露了 2026 年前三季度财务决算。"
                            "前三季度，国铁集团实现营业总收入 "
                            "9143 亿元，净利润盈利 130 亿元，多项运输指标创历史同期新高。同比增长 4.8%。",
                 "url": "https://www.ithome.com/0/1.htm", "published_at": "2026-10-31T02:10:00+00:00",
                 "origin_family": "ithome.com"}],
}
TITLE_EBOLA = "More than 5,000 die from Ebola"
TITLE_RAIL = "国铁集团：2026 年前三季度营收 9143 亿元"


def _zh(event, **kw) -> tuple[EventEnrichment, object]:
    ev = build_event_input(event, None, "World", event["entities"], edition_id="zh_cn")
    base = dict(event_id=event["event_id"], category="World", chinese_headline="标题",
                chinese_what_happened="占位。", supporting_source_ids=["s0"], support={})
    base.update(kw)
    return EventEnrichment(**base), ev


def _q(*quotes):
    return [{"source_id": "s0", "quote": q} for q in quotes]


def test_a_headline_only_figure_is_offered_and_accepted_when_quoted_from_the_title():
    e, ev = _zh(EBOLA, chinese_headline="刚果（金）埃博拉疫情超5000人死亡",
                chinese_what_happened="刚果（金）埃博拉疫情已有超过5000人死亡，超过1万人感染。",
                support={"headline": _q(TITLE_EBOLA),
                         "what": _q(TITLE_EBOLA, "more than 10,000 people have contracted")})
    assert ev.sources[0]["title"].startswith("More than 5,000")  # the title is evidence now
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


def test_a_figure_is_checked_against_its_own_field_quotes_not_the_whole_evidence_pool():
    # 5,000 is in the evidence (the title), but the what field quotes only the excerpt
    e, ev = _zh(EBOLA, chinese_headline="刚果（金）埃博拉疫情超5000人死亡",
                chinese_what_happened="刚果（金）埃博拉疫情已有超过5000人死亡。",
                support={"headline": _q(TITLE_EBOLA), "what": _q("more than 10,000 people have contracted")})
    r = validate_enrichment(e, ev)
    assert not r.ok and any("chinese_what_happened" in x and "5000" in x for x in r.reasons)


@pytest.mark.parametrize("text, why", [
    ("国铁集团前三季度营业总收入9143亿美元。", "unit"),           # 亿元 in the source, not 亿美元
    ("国铁集团前三季度营业总收入9000亿元。", "rounded"),          # coarser than the source (1.6% off)
    ("国铁集团前三季度营业总收入同比增长4.9%。", "small figure"),  # was skipped (<= 1 rule) only for <=1; 4.9 vs 4.8
    ("国铁集团前三季度净利润130亿元，增长0.5%。", "<= 1"),         # 0.5% appears nowhere -- no longer skipped
])
def test_wrong_unit_precision_or_small_numbers_are_rejected(text, why):
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元", chinese_what_happened=text,
                support={"headline": _q(TITLE_RAIL),
                         "what": _q("实现营业总收入 9143 亿元", "净利润盈利 130 亿元", "同比增长 4.8%")})
    r = validate_enrichment(e, ev)
    assert not r.ok, why


def test_an_english_gloss_after_a_chinese_name_is_rejected_with_an_actionable_reason():
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团（China State Railway Group）前三季度营业总收入9143亿元。",
                entities=["国铁集团 (China State Railway Group)"],
                support={"headline": _q(TITLE_RAIL), "what": _q("实现营业总收入 9143 亿元")})
    r = validate_enrichment(e, ev)
    assert not r.ok
    assert any("remove the parenthesised Latin text" in x for x in r.reasons)
    # the bracketed part is checked on its own, never accepted by stripping the brackets
    assert any("entity 'China State Railway Group'" in x for x in r.reasons)
    assert not any("entity '国铁集团'" in x for x in r.reasons)


def test_the_same_story_without_the_gloss_passes():
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团10月30日披露，前三季度营业总收入9143亿元，净利润130亿元。",
                entities=["国铁集团"], chinese_brief_line="国铁集团前三季度营收9143亿元。",
                support={"headline": _q(TITLE_RAIL),
                         "what": _q("10 月 30 日，国铁集团披露了", "实现营业总收入 9143 亿元",
                                                                "净利润盈利 130 亿元"),
                         "brief": _q("实现营业总收入 9143 亿元")})
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


def test_a_latin_name_the_quotes_do_not_contain_is_rejected():
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团与Siemens合作，前三季度营业总收入9143亿元。",
                support={"headline": _q(TITLE_RAIL), "what": _q("实现营业总收入 9143 亿元")})
    r = validate_enrichment(e, ev)
    assert not r.ok and any("'Siemens'" in x for x in r.reasons)


def test_comments_on_what_the_sources_lack_are_rejected():
    e, ev = _zh(EBOLA, chinese_headline="刚果（金）埃博拉疫情超5000人死亡",
                chinese_what_happened="刚果（金）暴发埃博拉疫情，来源未提供具体死亡人数。",
                support={"headline": _q(TITLE_EBOLA), "what": _q("contracted the rare virus")})
    r = validate_enrichment(e, ev)
    assert not r.ok and any("comments on what the sources do not say" in x for x in r.reasons)


def test_a_quote_that_is_not_verbatim_is_rejected():
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元", chinese_what_happened="国铁集团营收9143亿元。",
                support={"headline": _q(TITLE_RAIL), "what": _q("国铁集团营收达到 9143 亿元")})
    r = validate_enrichment(e, ev)
    assert not r.ok and any("not verbatim" in x for x in r.reasons)


def test_why_line_needs_a_kind_and_a_judgment_needs_its_owner():
    common = dict(chinese_headline="国铁集团前三季度营收9143亿元",
                  chinese_what_happened="国铁集团前三季度营业总收入9143亿元。")
    sup = {"headline": _q(TITLE_RAIL), "what": _q("实现营业总收入 9143 亿元"), "why": _q("多项运输指标创历史同期新高")}
    e, ev = _zh(RAIL, chinese_takeaway="多项运输指标创历史同期新高。", support=sup, **common)
    assert any("why_kind" in x for x in validate_enrichment(e, ev).reasons)
    e, ev = _zh(RAIL, chinese_takeaway="多项运输指标创历史同期新高。", why_kind="source_judgment",
                support=sup, **common)
    assert any("whose judgment" in x for x in validate_enrichment(e, ev).reasons)
    e, ev = _zh(RAIL, chinese_takeaway="国铁集团称，多项运输指标创历史同期新高。", why_kind="reported",
                support=sup, **common)
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


def test_dates_must_be_quoted_or_a_source_date():
    sup = {"headline": _q(TITLE_RAIL), "what": _q("实现营业总收入 9143 亿元")}
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团11月5日披露，前三季度营业总收入9143亿元。", support=sup)
    assert any("11月5日" in x for x in validate_enrichment(e, ev).reasons)
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团10月31日披露，前三季度营业总收入9143亿元。",
                support=sup)  # 10-31 is the publication date
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


class _Slow:
    model = "fake"
    timeout = 60.0

    def __init__(self):
        self.calls = 0
        self.timeouts = []

    def enrich_events(self, events):
        self.calls += 1
        self.timeouts.append(self.timeout)
        raise LLMUnavailableError("down")


def test_no_call_is_started_without_enough_run_budget_and_the_call_timeout_shrinks_to_it():
    p = _Slow()
    r = editorial_enrichment.enrich_story(RAIL, None, "World", [], edition_id="zh_cn", provider=p,
                                          deadline=time.monotonic() + 5)
    assert p.calls == 0 and r.fallback_reason == "editorial run budget exhausted"
    p = _Slow()
    editorial_enrichment.enrich_story(RAIL, None, "World", [], edition_id="zh_cn", provider=p,
                                      deadline=time.monotonic() + 30)
    assert p.calls == 1 and p.timeouts[0] <= 30 and p.timeout == 60.0  # restored afterwards


def test_json_repair_is_skipped_when_the_run_deadline_is_near(monkeypatch):
    from news_intel.llm.hermes_cli import HermesCliProvider

    p = HermesCliProvider("/bin/true", timeout=60)
    monkeypatch.setattr(p, "_run_once", lambda prompt: "not json")
    p.call_deadline = time.monotonic() + 5
    with pytest.raises(LLMUnavailableError, match="no time left for a JSON repair"):
        p._complete("x")


def test_reviewed_aliases_have_a_basis_and_short_latin_aliases_match_whole_words_only():
    from news_intel.pipeline.editorial_validation import _name_supported
    from news_intel.pipeline.entity_aliases import alias_entries, alias_variants

    assert all(e["basis"] in ("source", "official", "replay", "short_form") and e["ref"] for e in alias_entries())
    assert "英特尔" in alias_variants("Intel") and "intel" in alias_variants("英特尔")
    assert _name_supported("英特尔", "Intel shares fell 30% at the open")
    assert _name_supported("联合国", "The UN said on Friday")
    assert not _name_supported("联合国", "a fundamental shift in unclear markets")  # "un" inside words
    assert not _name_supported("世界卫生组织", "the whole region")  # "who" inside "whole"
    assert not _name_supported("Fabricated Corp", "Intel shares fell")


def test_an_aliased_chinese_name_for_an_english_source_passes_and_an_unknown_one_does_not():
    intel = {
        "event_id": "E-intel", "title": "Intel shares plunge 30% after weak forecast", "excerpt": "", "entities": [],
        "sources": [{"publisher": "BBC News", "raw_title": "Intel shares plunge 30% after weak forecast",
                     "excerpt": "", "url": "https://bbc.test/i", "published_at": "2026-10-30T20:00:00+00:00"}],
    }
    sup = {"headline": _q("Intel shares plunge 30%"), "what": _q("Intel shares plunge 30% after weak forecast")}
    e, ev = _zh(intel, chinese_headline="英特尔股价暴跌30%", chinese_what_happened="英特尔业绩指引疲弱，股价暴跌30%。",
                entities=["英特尔"], support=sup)
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons
    e, ev = _zh(intel, chinese_headline="英特尔股价暴跌30%", chinese_what_happened="英特尔业绩指引疲弱，股价暴跌30%。",
                entities=["英特尔", "台湾积体电路"], support=sup)
    assert any("台湾积体电路" in x for x in validate_enrichment(e, ev).reasons)


def test_a_chinese_bracket_is_part_of_the_name_and_drc_resolves_through_its_reviewed_alias():
    e, ev = _zh(EBOLA, chinese_headline="刚果（金）埃博拉疫情超5000人死亡", entities=["刚果（金）"],
                chinese_what_happened="刚果（金）埃博拉疫情已有超过5000人死亡。",
                support={"headline": _q(TITLE_EBOLA), "what": _q(TITLE_EBOLA)})
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


def test_country_names_resolve_through_the_un_lists_and_acronyms_need_capitals():
    from news_intel.pipeline.editorial_validation import _name_supported
    from news_intel.pipeline.entity_aliases import alias_entries

    assert any(e["basis"] == "official" and e["zh"] == "中国" for e in alias_entries())
    assert _name_supported("中国", "China denies allegations of a spy operation")  # 中国 = UN "China (...)" = "China"
    assert _name_supported("美国", "US officials said") and _name_supported("美国", "the U.S. said")
    assert not _name_supported("美国", "they told us on Friday")  # the pronoun is not the country
    assert _name_supported("俄罗斯", "Russia's foreign minister")
    assert _name_supported("刚果（金）", "in DR Congo's east")


def test_an_interpretation_labelled_reported_is_refused():
    sup = {"headline": _q(TITLE_RAIL), "what": _q("实现营业总收入 9143 亿元"), "why": _q("多项运输指标创历史同期新高")}
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团前三季度营业总收入9143亿元。",
                chinese_takeaway="多项运输指标创历史同期新高，反映出铁路需求旺盛。", why_kind="reported",
                support=sup)
    assert any("source_judgment" in x for x in validate_enrichment(e, ev).reasons)


def test_short_chinese_attribution_quotes_are_accepted_and_hedge_feedback_offers_a_way_out():
    e, ev = _zh(RAIL, chinese_headline="国铁集团前三季度营收9143亿元",
                chinese_what_happened="国铁集团披露，前三季度营业总收入9143亿元。",
                support={"headline": _q(TITLE_RAIL), "what": _q("国铁集团披露", "实现营业总收入 9143 亿元")})
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons
    from news_intel.pipeline.editorial_validation import _quote_min

    assert _quote_min("知情人士称") == 4 and _quote_min("sources") == 6


def test_the_offered_publisher_may_be_named_and_the_brief_line_rests_on_the_items_own_quotes():
    ebola_bbc = {**EBOLA, "sources": [dict(EBOLA["sources"][0], publisher="BBC")]}
    e, ev = _zh(ebola_bbc, chinese_headline="刚果（金）埃博拉疫情超5000人死亡",
                chinese_what_happened="据BBC报道，刚果（金）埃博拉疫情已有超过5000人死亡。",
                chinese_brief_line="BBC：刚果（金）埃博拉疫情超5000人死亡。",
                support={"headline": _q(TITLE_EBOLA), "what": _q(TITLE_EBOLA), "brief": _q("deadliest outbreak")})
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons
    # but never a name or figure found in none of the item's quotes
    e2 = EventEnrichment(**{**e.model_dump(), "chinese_brief_line": "WHO称刚果（金）埃博拉疫情超6000人死亡。"})
    assert len(validate_enrichment(e2, ev).reasons) >= 2


def test_a_chinese_rendering_needs_the_sources_own_spelling_and_otherwise_stays_refused():
    quad = {
        "event_id": "E-quad", "title": "Quad not 'anti-China', says Rubio as first summit in two years nears",
        "excerpt": "US Secretary of State Marco Rubio said on Saturday the Quad is not an anti-China grouping.",
        "entities": [],
        "sources": [{"publisher": "South China Morning Post — China",
                     "raw_title": "Quad not 'anti-China', says Rubio as first summit in two years nears",
                     "excerpt": "US Secretary of State Marco Rubio said on Saturday the Quad is not an anti-China "
                                "grouping.",
                     "url": "https://scmp.test/q", "published_at": "2026-10-31T09:00:00+00:00"}],
    }
    q = _q("Quad not 'anti-China', says Rubio")
    base = dict(chinese_headline="鲁比奥称四方安全对话并非反华联盟",
                chinese_what_happened="美国国务卿鲁比奥称，四方安全对话不是反华联盟。",
                support={"headline": q, "what": q}, entities=["鲁比奥", "四方安全对话"])
    e, ev = _zh(quad, **base)  # no renderings: refused, as before
    assert any("鲁比奥" in x for x in validate_enrichment(e, ev).reasons)
    ok = [{"zh": "鲁比奥", "source_name": "Marco Rubio", "source_id": "s0"},
          {"zh": "四方安全对话", "source_name": "Quad", "source_id": "s0"}]
    e, ev = _zh(quad, name_renderings=ok, **base)
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons
    bad = [{"zh": "鲁比奥", "source_name": "Mark Rubio", "source_id": "s0"}, ok[1]]
    e, ev = _zh(quad, name_renderings=bad, **base)
    assert any("'Mark Rubio': that spelling is not in source" in x for x in validate_enrichment(e, ev).reasons)


QUAKE = {
    "event_id": "E-quake", "title": "地震造成120人死亡",
    "excerpt": "当地政府10月31日称，地震造成120人死亡、9,999人受伤，约30%的房屋受损，救援将持续两天。"
               "An official said thousands of people were evacuated. It is the deadliest quake since the 1970s, "
               "and 2026 output of the region's SU7 plant is halted.",
    "entities": [],
    "sources": [{"publisher": "Example", "raw_title": "地震造成120人死亡",
                 "excerpt": "当地政府10月31日称，地震造成120人死亡、9,999人受伤，约30%的房屋受损，救援将持续两天。"
                            "An official said thousands of people were evacuated. It is the deadliest quake "
                            "since the 1970s, and 2026 output of the region's SU7 plant is halted.",
                 "url": "https://example.test/q", "published_at": "2026-10-31T09:00:00+00:00"}],
}
Q_ALL = "当地政府10月31日称，地震造成120人死亡、9,999人受伤，约30%的房屋受损，救援将持续两天。"


@pytest.mark.parametrize("what, why", [
    ("地震造成2026人死亡。", "a count written like a year is a count, not a year (2026-11-02 review)"),
    ("地震造成九千九百九十九人死亡。", "Chinese numerals are figures too (2026-11-02 review; source: 120 dead)"),
    ("地震造成一百二十一人死亡。", "Chinese numeral, wrong by one"),
    ("约四成房屋受损。", "成 is a percentage: 40% is not 30%"),
    ("救援将持续三天。", "a duration is a figure: 3 days is not two"),
    ("数万人被疏散。", "a vague quantity needs the same vague class: tens of thousands is not thousands"),
    ("地震造成三四百人死亡。", "an unreadable figure is refused, never skipped"),
    ("这是2019年以来最严重的地震。", "a year must be quoted or a source's own year"),
])
def test_fabricated_figures_in_any_notation_are_refused(what, why):
    what_quotes = (_q("地震造成120人死亡") if "九千" in what or "2026" in what else
                   _q(Q_ALL, "thousands of people were evacuated", "the deadliest quake since the 1970s"))
    e, ev = _zh(QUAKE, chinese_headline="地震造成120人死亡", chinese_what_happened=what,
                support={"headline": _q("地震造成120人死亡"), "what": what_quotes})
    assert not validate_enrichment(e, ev).ok, why


def test_a_true_figure_of_another_claim_is_not_caught_deterministically():
    """Known limit, recorded: 9,999 is in the quote (injured), so '九千九百九十九人死亡' matches by value and unit.
    Which claim a figure belongs to is the brief verifier's check; the dailies have none."""
    e, ev = _zh(QUAKE, chinese_headline="地震造成120人死亡", chinese_what_happened="地震造成九千九百九十九人死亡。",
                support={"headline": _q("地震造成120人死亡"), "what": _q(Q_ALL)})
    assert validate_enrichment(e, ev).ok


@pytest.mark.parametrize("what", [
    "地震造成一百二十人死亡，九千九百九十九人受伤。",          # Chinese numerals for the source's Arabic figures
    "约三成房屋受损，救援将持续两天。",                        # 成 and 两天 for 30% and 两天
    "数千人被疏散，这是1970年代以来最严重的地震。",            # thousands; the 1970s
    "当地政府10月31日称，该地区SU7工厂2026年的生产已停止。",   # a date, a model name, a year in a year context
    "一名官员称，地震造成120人死亡。",                         # 一名 as the article
])
def test_true_equivalents_and_legal_dates_names_and_years_still_pass(what):
    quotes = _q(Q_ALL, "An official said thousands of people were evacuated", "the deadliest quake since the 1970s",
                "2026 output of the region's SU7 plant is halted")
    e, ev = _zh(QUAKE, chinese_headline="地震造成120人死亡", chinese_what_happened=what,
                support={"headline": _q("地震造成120人死亡"), "what": quotes})
    r = validate_enrichment(e, ev)
    assert r.ok, r.reasons


def test_english_number_words_count_both_ways():
    from news_intel.pipeline.editorial_validation import numeric_claims

    assert [c.value for c in numeric_claims("within two days", source=True)] == [2.0]
    assert [c.value for c in numeric_claims("twenty-five people")] == [25.0]
    assert numeric_claims("one of the largest") == []  # the article, in written text
