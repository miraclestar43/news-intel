"""2026-08-29 production incident (Aug 28 zh_cn digest, DELIVERED): a
purely DOMESTIC Chinese story -- the Chengdu Hi-Tech Disaster Reduction
Institute apologising for a magnitude error in an earthquake early warning
-- was classified "US / China / World" and rendered to the reader under the
国际与中美 (International & US-China) section.

Reproduction: its only china-group hit was the generic "中国" (appearing
inside the DOMESTIC institution names 中国地震预警网 / 中国地震局), and the
only thing satisfying the international-signal requirement was an
incidental "美国" mention in the body. A bare foreign country name is
exactly the "bare mention proves nothing" problem _CHINA_GENERIC_TERMS
already guards against on the China side -- it simply was not applied
symmetrically."""

from __future__ import annotations

import pytest

from news_intel.pipeline.relevance import _china_policy_bucket
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT

# Verbatim from the production raw_items row behind the delivered story.
CHENGDU_DOMESTIC = (
    "成都高新减灾研究所发第四份声明：首报预警震级偏差大表示道歉，但该预警不是误报 "
    "IT之家 8 月 26 日消息，成都高新减灾研究所今晚再发声明。 "
    "减灾所未冒用“中国地震预警网”，因为中国地震局与减灾所的合作备忘录及中国地震预警网的共建关系依然有效。"
    "该所同时提到美国等国家的地震预警实践作为对比。"
)


@pytest.fixture
def zh_topics(tmp_path):
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg.topics_for("zh_cn")
    reset_config_cache()


def _bucket(text, topics):
    return _china_policy_bucket(text, text.lower(), topics)


def test_domestic_chinese_story_is_not_classified_us_china_world(zh_topics):
    """The exact delivered regression."""
    assert _bucket(CHENGDU_DOMESTIC, zh_topics) is None


def test_bare_foreign_country_mention_alone_does_not_internationalise_a_domestic_story(zh_topics):
    assert _bucket("中国某公司发布新款手机，外观设计参考了美国流行趋势。", zh_topics) is None


@pytest.mark.parametrize(
    "text",
    [
        "美国宣布对中国实施新的芯片出口管制措施。",
        "中国对美国商品加征关税，贸易战升级。",
        "中美关系出现新的变化，双方举行会谈。",
        "美国将多家中国公司列入实体清单并实施制裁。",
    ],
)
def test_genuine_us_china_stories_still_classify(text, zh_topics):
    """The tightening must not cost real US-China coverage."""
    assert _bucket(text, zh_topics) is not None
