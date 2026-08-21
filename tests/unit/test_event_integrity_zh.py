"""pipeline/event_integrity_zh.py: Chinese-language event-type/consequence
gates, the zh_cn counterpart to pipeline/event_integrity.py. Real defect
found via a live-collection replay (Aug 19/20 zh_cn digests, already
delivered): jobs/daily.py's _has_material_consequence() called the
shared, English-phrase-based event_integrity.is_low_consequence_event_type()
for zh_cn candidates too -- which never matches Chinese text at all, so
conference-appearance and award-list stories reached selection with
nothing behind the headline."""

from __future__ import annotations

from news_intel.pipeline.event_integrity_zh import (
    is_conference_appearance_without_event_zh,
    is_low_consequence_event_type_zh,
    is_unqualified_recognition_story_zh,
)

_WRC_TITLE = "章鱼动力亮相WRC 2026，携脑-手-数据技术体系探索具身智能未来范式"
_WRC_EXCERPT = "章鱼动力亮相WRC 2026，携脑-手-数据技术体系探索具身智能未来范式。"

_AWARD_TITLE = "IDC发布2026中国AI50强：360以智能体+安全双轮驱动入选"
_AWARD_EXCERPT = "IDC发布2026中国AI50强：360以智能体+安全双轮驱动入选。"


def test_wrc_conference_appearance_without_event_fails_the_gate():
    assert is_conference_appearance_without_event_zh(_WRC_TITLE, _WRC_EXCERPT) is True
    assert is_low_consequence_event_type_zh(_WRC_TITLE, _WRC_EXCERPT) is True


def test_360_award_list_inclusion_fails_the_gate():
    assert is_unqualified_recognition_story_zh(_AWARD_TITLE, _AWARD_EXCERPT) is True
    assert is_low_consequence_event_type_zh(_AWARD_TITLE, _AWARD_EXCERPT) is True


def test_conference_appearance_with_a_genuine_material_event_is_not_excluded():
    """General event-type logic, not a title-specific carve-out: a
    conference appearance accompanied by a real funding/contract
    announcement is not routine."""
    title = "某机器人公司亮相WRC 2026，现场宣布获得10亿元融资并与某车企签署量产合同"
    excerpt = "该公司在WRC 2026上亮相，同时宣布完成10亿元融资，并与某车企签署量产合同。"
    assert is_conference_appearance_without_event_zh(title, excerpt) is False
    assert is_low_consequence_event_type_zh(title, excerpt) is False


def test_award_story_with_a_genuine_material_consequence_is_not_excluded():
    title = "某公司入选2026中国AI50强，同时宣布完成新一轮20亿元融资"
    excerpt = "该公司入选榜单，并宣布完成新一轮20亿元融资。"
    assert is_unqualified_recognition_story_zh(title, excerpt) is False


def test_ordinary_policy_story_without_gate_vocabulary_is_unaffected():
    title = "国家发展改革委发布人工智能产业发展指导意见"
    excerpt = "国家发展改革委发布指导意见，加大对先进制程芯片的政策支持力度，涉及资金规模约500亿元。"
    assert is_low_consequence_event_type_zh(title, excerpt) is False
