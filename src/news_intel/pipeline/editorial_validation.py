"""Deterministic post-generation validators for the optional per-story LLM
editorial layer (2026-08-24). An LLM's structured output is a PROPOSAL,
never trusted directly -- every claim is checked against the same evidence
packet (pipeline.editorial_evidence.build_event_input) that was offered to
the model before the output is allowed to reach a digest. A validation
failure never raises; callers get a list of human-readable reasons and
decide whether to retry-with-feedback or fall back.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from news_intel.models import EventEnrichment, EventInput
from news_intel.pipeline.editorial_evidence import _UNCERTAINTY_MARKERS_EN, _UNCERTAINTY_MARKERS_ZH
from news_intel.pipeline.entity_aliases import alias_variants
from news_intel.pipeline.summary_quality import _ends_with_dangling_clause, _is_abbreviation_period
from news_intel.pipeline.zh_text import parse_zh_amounts

# 2026-08-24: the exact banned patterns named across this incident's
# multiple review rounds -- checked against EVERY text field the model
# returns, not just why_it_matters, so a banned phrase can't slip through
# via chinese_takeaway or what_happened instead.
_BANNED_PATTERNS = [
    re.compile(r"ranks among today'?s? most material", re.IGNORECASE),
    re.compile(r"this development involving", re.IGNORECASE),
    re.compile(r"a material development in", re.IGNORECASE),
    re.compile(r"属.{0,20}重要动态$"),
    re.compile(r"newly named \("),
    re.compile(r"^worth \$", re.IGNORECASE),  # "worth $X" as the ENTIRE clause -- a bare, roleless amount
    # 2026-09-07 production incident (Sep 5 en_global, DELIVERED): the
    # Insider Monkey item's "why it matters" was "This trend could indicate
    # that investor interest ... potentially affecting valuations across
    # chip manufacturing equipment and materials companies." That is a
    # tautology -- any market story "could affect valuations" -- not a
    # story-specific consequence. Generic hedged market-implication
    # boilerplate is rejected the same way the category-label templates are.
    re.compile(r"could (?:affect|impact|influence) (?:valuations|share prices|stock prices|sentiment)",
               re.IGNORECASE),
    re.compile(r"potentially affecting valuations", re.IGNORECASE),
    re.compile(r"may (?:affect|impact) (?:valuations|investor sentiment)", re.IGNORECASE),
    re.compile(r"或将?影响.{0,10}估值"),
    re.compile(r"可能会?影响.{0,10}估值"),
]

_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


_ZH_MAGNITUDE_SUFFIXES = ("万亿", "亿元", "亿美元", "亿", "万元", "万美元", "万")

# 2026-08-25 repair: real live-model replay found a genuine packaging gap,
# not a model hallucination -- a raw source excerpt said "710M shares"; the
# model (correctly) wrote the number out in full as "710,000,000", but
# _numbers_in() scanning that SAME raw excerpt for evidence only ever
# extracted the bare "710" (no magnitude handling for English M/B/T
# suffixes, unlike the 万/亿 handling already here for Chinese), so a
# real, traceable figure was rejected as unsupported. Mirrors the existing
# Chinese magnitude-suffix pattern: skip the bare digit match and
# contribute only the correctly scaled value.
_EN_MAGNITUDE_WORD_RE = re.compile(r"^\s?(million|billion|trillion|mn|bn|tn)\b", re.IGNORECASE)
_EN_MAGNITUDE_LETTER_RE = re.compile(r"^(M|B|T)\b")
_EN_MAGNITUDE_MULTIPLIERS = {
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "trillion": 1_000_000_000_000, "tn": 1_000_000_000_000, "t": 1_000_000_000_000,
}


def _numbers_in(text: str) -> set[float]:
    """Both plain numbers (English/bare figures) and Chinese-magnitude
    figures (2.5亿 -> 250,000,000) -- a Chinese takeaway field routinely
    expresses the SAME evidence number using 万/亿 notation, which must
    resolve to the same underlying value as the English fields' plain
    digits, not be flagged as a second, untraceable number. A bare digit
    match immediately followed by a magnitude suffix (Chinese 万/亿 or
    English million/billion/trillion/M/B/T) is skipped in its bare form
    and contributes only the correctly scaled value -- so "2.5亿" or
    "710M" never ALSO contributes a second, wrong bare 2.5/710."""
    text = text or ""
    found = set()
    for match in _NUMBER_RE.finditer(text):
        # 2026-10-31 (zh_cn fallback diagnosis, real IT之家 text): "32 万亿日元" -- a space before the magnitude, and
        # a currency other than 元/美元 -- yielded only a bare 32 (with the space) or nothing at all (without it), so a
        # model sentence stating the source's own 32 trillion yen was rejected as untraceable. Scale by 万亿/亿/万
        # whatever follows, allowing whitespace in between.
        tail = text[match.end() : match.end() + 6].lstrip()
        if any(tail.startswith(suffix) for suffix in _ZH_MAGNITUDE_SUFFIXES):
            try:
                base = float(match.group(0).replace(",", ""))
            except ValueError:
                continue
            scale = 1e12 if tail.startswith("万亿") else 1e8 if tail.startswith("亿") else 1e4
            found.add(base * scale)
            continue
        raw = match.group(0).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        remainder = text[match.end() : match.end() + 10]
        word_match = _EN_MAGNITUDE_WORD_RE.match(remainder)
        letter_match = None if word_match else _EN_MAGNITUDE_LETTER_RE.match(remainder)
        if word_match:
            value *= _EN_MAGNITUDE_MULTIPLIERS[word_match.group(1).lower()]
        elif letter_match:
            value *= _EN_MAGNITUDE_MULTIPLIERS[letter_match.group(1).lower()]
        found.add(value)
    found |= set(parse_zh_amounts(text))
    return found


class ValidationResult:
    def __init__(self, ok: bool, reasons: list[str]):
        self.ok = ok
        self.reasons = reasons

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------------------------------------------
# 2026-11-01 (news-intel v2): claim-level grounding.
#
# The old checks pooled every number of every source into one set, skipped numbers <= 1 and accepted anything within
# 1% of any pooled number -- so a figure that belonged to another company, another period or another unit passed as
# long as the same digits appeared somewhere. Each reader-facing field group now carries verbatim quotes (support)
# and is checked against ITS OWN quotes: the number, its unit/currency and its written precision. This is still not
# fact verification -- relation, attribution, certainty and headline/body agreement are re-checked by an
# independent verifier pass in the brief (brief/verify.py); what cannot be verified is removed or rejected there.
# ---------------------------------------------------------------------------------------------------------------

_FIELD_GROUPS = {
    "headline": ("english_headline", "chinese_headline"),
    "what": ("what_happened", "chinese_what_happened"),
    "why": ("why_it_matters", "chinese_takeaway"),
    "brief": ("chinese_brief_line",),
}
_ZH_FIELDS = {"chinese_headline", "chinese_what_happened", "chinese_takeaway", "chinese_brief_line"}
_WHY_KINDS = {"reported", "source_judgment"}
_HEDGE_RE = re.compile(
    r"(或将|据报道|消息称|据悉|传闻|疑似|可能|据称|知情人士|尚未证实|未经证实|初步|拟|计划|考虑|洽谈|谈判|预计|或|"
    r"据[^，。]{1,16}(称|报道|消息|透露|表示)|"
    r"\b(could|might|may|reportedly|reported|according to|allegedly|alleged|considering|discussed|plans?|planned|"
    r"in talks|expected|potential|said|says|claims?|unconfirmed|preliminary)\b)",
    re.IGNORECASE,
)
_INTERPRETIVE = re.compile(
    r"(反映出|反映了|意味着|表明|显示出|凸显|标志着|预示|"
    r"shows that|signals?|suggests?|underscores?|highlights?|reflects?|means that)",
    re.IGNORECASE,
)
_QUOTE_MIN, _QUOTE_MAX = 6, 200


def _quote_min(quote: str) -> int:
    """A Chinese attribution such as 知情人士称 is a complete quote in five characters (replay 2026-10-31)."""
    return 4 if re.search(r"[一-鿿]", quote) else _QUOTE_MIN

# Comments on what the sources lack are review notes, not news (2026-10-31: an Ebola item said the source gave no
# death toll while its own headline gave one).
_META_COMMENTARY = [
    re.compile(r"(来源|报道|原文|消息|文章|资料)(中|里)?(并)?(未|没有|没)(有)?(提供|给出|说明|披露|提及|透露|写明|交代)"),
    re.compile(r"(未|没有)(提供|给出|披露)(具体)?(的)?(数字|数据|人数|细节)"),
    re.compile(
        r"\b(the )?(sources?|reports?|articles?) (do|does|did)(n't| not) (say|give|specify|mention|provide|state)",
        re.IGNORECASE,
    ),
]

# Generic terms that are not names; they may appear in Latin letters in Chinese text without a quote. Reviewable list.
_GENERIC_LATIN = {
    "ai", "ceo", "cfo", "cto", "gdp", "cpi", "ppi", "pmi", "ipo", "etf", "app", "it", "ev", "5g", "6g", "cpu", "gpu",
    "npu", "dram", "nand", "hbm", "pc", "tv", "vr", "ar", "api", "q1", "q2", "q3", "q4", "h1", "h2", "vs", "ok",
}
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&+'’.\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9&+'’.\-]*)*")
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[)）]")

_CURRENCY_WORDS = [
    ("元人民币", "CNY"), ("人民币", "CNY"), ("美元", "USD"), ("日元", "JPY"), ("韩元", "KRW"), ("欧元", "EUR"),
    ("英镑", "GBP"), ("港元", "HKD"), ("港币", "HKD"), ("新台币", "TWD"), ("台币", "TWD"), ("卢布", "RUB"),
    ("卢比", "INR"), ("元", "CNY"),
]
_CURRENCY_EN = {
    "dollars": "USD", "dollar": "USD", "usd": "USD", "yuan": "CNY", "renminbi": "CNY", "rmb": "CNY", "cny": "CNY",
    "yen": "JPY", "won": "KRW", "euros": "EUR", "euro": "EUR", "eur": "EUR", "pounds": "GBP", "gbp": "GBP",
    "rupees": "INR", "roubles": "RUB", "rubles": "RUB",
}
_CURRENCY_SYMBOLS = [("US$", "USD"), ("HK$", "HKD"), ("NT$", "TWD"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
                     ("¥", "CNY/JPY"), ("￥", "CNY/JPY")]
_ZH_MAG = [("万亿", 1e12), ("亿", 1e8), ("万", 1e4), ("千", 1e3)]
_EN_MAG = {"thousand": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
           "trillion": 1e12, "tn": 1e12, "t": 1e12, "k": 1e3}
_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![A-Za-z]*\d)")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass(frozen=True)
class NumberClaim:
    value: float
    unit: str | None  # CNY/USD/... | pct | pp | times | bps | people | days/years/... | vague:<class> | None
    tolerance: float  # half a unit of the last written digit
    text: str
    article: bool = False  # 一 + a measure word other than 人/名: the indefinite article, also "a"/"an" in a source
    what: str | None = None  # dead | injured | infected | missing | arrested | evacuated: the casualty word after it


# A casualty figure must keep its casualty class (2026-11-02: '9,999人受伤' in the quote must not support
# '九千九百九十九人死亡'). Read in the few characters after the figure and its unit.
_CASUALTY = [
    ("dead", r"(死亡|遇难|丧生|罹难|身亡|死|killed|dead|died|deaths?|die)"),
    ("injured", r"(受伤|伤|injured|wounded|hurt)"),
    ("infected", r"(感染|确诊|病例|infected|contracted|infections|cases)"),
    ("missing", r"(失踪|missing)"),
    ("arrested", r"(被捕|被拘|逮捕|arrested|detained)"),
    ("evacuated", r"(疏散|撤离|转移|evacuated|displaced)"),
]


def _casualty_after(text: str, pos: int) -> str | None:
    window = text[pos : pos + 32].lower()
    window = re.sub(r"^\s*(万亿|亿|万|千|thousand|million)?\s*(多|余|以上|左右)?\s*"
                    r"(人次|人|名|people|persons|civilians|others)?\s*(have been|had been|were|are|have)?\s*",
                    "", window)
    for cls, pat in _CASUALTY:
        if re.match(pat, window):
            return cls
    return None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", "", s).lower()


def _unit_after(text: str, pos: int) -> tuple[float, str | None, int]:
    """(magnitude, unit, characters consumed) right after a number."""
    rest = text[pos : pos + 24]
    stripped = rest.lstrip()
    gap = len(rest) - len(stripped)
    mag = 1.0
    for word, m in _ZH_MAG:
        if stripped.startswith(word):
            mag, stripped, gap = m, stripped[len(word):], gap + len(word)
            break
    else:
        m_en = re.match(r"(thousand|million|billion|trillion|mn|bn|tn)\b|([MBTK])(?![A-Za-z])", stripped,
                        re.IGNORECASE)
        if m_en and (m_en.group(1) or m_en.group(2)):
            key = (m_en.group(1) or m_en.group(2)).lower()
            mag, stripped, gap = _EN_MAG[key], stripped[m_en.end():], gap + m_en.end()
    s2 = stripped.lstrip()
    for word, code in _CURRENCY_WORDS:
        if s2.startswith(word):
            return mag, code, gap
    m_cur = re.match(r"(?:US\s)?([A-Za-z]+)", s2)
    if m_cur and m_cur.group(1).lower() in _CURRENCY_EN:
        return mag, _CURRENCY_EN[m_cur.group(1).lower()], gap
    if s2.startswith(("%", "％")) or re.match(r"(percent|per cent)\b", s2, re.IGNORECASE):
        return mag, "pct", gap
    if s2.startswith("个百分点") or re.match(r"percentage points?\b", s2, re.IGNORECASE):
        return mag, "pp", gap
    if s2.startswith("个基点") or re.match(r"(basis points?|bps)\b", s2, re.IGNORECASE):
        return mag, "bps", gap
    if s2.startswith("倍") or re.match(r"(times|-fold|fold)\b", s2, re.IGNORECASE):
        return mag, "times", gap
    if s2.startswith(("人次", "人", "名", "people", "persons")):
        return mag, "people", gap
    for words, cls in _DURATIONS:
        if s2.startswith(words[0]) or re.match(words[1], s2, re.IGNORECASE):
            return mag, cls, gap
    return mag, None, gap


_YEAR_BEFORE = re.compile(r"\b(in|since|by|from|until|till|before|after|fiscal|fy|year|during|of|early|late|mid|"
                          r"end of|start of)\s*$", re.IGNORECASE)
_YEAR_ZH_AFTER = ("年", "财年", "年度")


def _year_context(text: str, start: int, end: int) -> bool:
    """A 1900-2099 figure that is a YEAR here: 2026年 / 2027财年, "in 2026", "fiscal 2027", "Oct 31, 2026",
    "2026-27". Anywhere else it is a quantity and is checked as one (2026-11-02 review: "地震造成2026人死亡" passed)."""
    if not re.fullmatch(r"(19|20)\d{2}", text[start:end]):
        return False
    after = text[end : end + 4].lstrip()
    if after.startswith(_YEAR_ZH_AFTER) or re.match(r"[-–/]\d{2}\b", text[end : end + 4]) or text[end : end + 1] == "s":
        return True
    before = text[max(0, start - 16) : start]
    if _YEAR_BEFORE.search(before.rstrip(" ,")):
        return True
    return bool(re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s*$", before,
                          re.IGNORECASE))


def _is_date_part(text: str, start: int, end: int) -> bool:
    """Month, day and clock-time digits (checked by the date rules), and years in a year context (checked by the year
    rule). A figure before 年 that is not a year is a duration (4年), and 个月 / 天 / 小时 are durations: quantities."""
    if _year_context(text, start, end):
        return True
    after = text[end : end + 3].lstrip()
    value = int(text[start:end]) if text[start:end].isdigit() else -1
    if after[:1] == "年":
        return False  # 4年, 30年: a duration
    if after[:1] == "月" and 1 <= value <= 12:
        return True
    if after[:1] in ("日", "号") and 1 <= value <= 31:
        return True
    if after[:1] in ("时", "点") and 0 <= value <= 24:
        return True
    if re.match(r"\s*[:：]\d{2}", text[end : end + 4]):  # 14:00
        return True
    before = text[max(0, start - 1) : start]
    if before in (":", "："):
        return True
    window = text[max(0, start - 10) : end + 10]
    if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*$", text[max(0, start - 10):start],
                 re.IGNORECASE):
        return True
    if re.match(r"\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", text[end : end + 6], re.IGNORECASE):
        return True
    del window
    return False


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
              "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 1e4, "亿": 1e8}
# A Chinese numeral is a figure claim when a measure word or unit follows it (or 百分之 precedes it). Reviewable list.
_CN_CLASSIFIERS = (
    "个月", "小时", "分钟", "星期", "公里", "平方", "人次", "美元", "日元", "欧元", "英镑", "韩元", "港元", "卢布",
    "元", "人", "名", "位", "个", "家", "起", "次", "件", "艘", "架", "辆", "座", "项", "条", "所", "国", "岁", "倍",
    "成", "吨",
    "米", "枚", "颗", "批", "轮", "届", "场", "份", "股", "只", "头", "例", "年", "天", "周", "%", "％",
)
_CN_NUM_RE = re.compile(r"(百分之)?([零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二三四五六七八九]+)?)(余|多)?")
_CN_NOT_NUMBER = ("一起", "一直", "一定", "一样", "一般", "一致", "一切", "一同", "一度", "一再", "一边", "一旦",
                  "一些",
                  "一批评", "一手", "一时", "万一", "千万", "一个月来", "十分", "百分百", "一方面", "两岸")
_VAGUE = [  # (class, Chinese forms, English pattern)
    ("tens", ("数十", "几十"), r"\bdozens\b|\btens of\b(?! thousands)"),
    ("hundreds", ("数百", "几百", "上百"), r"\bhundreds\b"),
    ("thousands", ("数千", "几千", "上千"), r"\bthousands\b"),
    ("tens_of_thousands", ("数万", "几万", "上万"), r"\btens of thousands\b"),
    ("millions", ("数百万", "几百万", "上百万"), r"\bmillions\b"),
]
_EN_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_EN_WORDS |= {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
              "dozen": 12}
_EN_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_EN_WORDS, key=len, reverse=True)) + r")(?:-(one|two|three|four|five|"
                         r"six|seven|eight|nine))?\b", re.IGNORECASE)
_DURATIONS = [  # (Chinese prefix, English pattern) -> class; checked after the other units
    (("个月", r"months?\b"), "months"), (("小时", r"hours?\b"), "hours"), (("分钟", r"minutes?\b"), "minutes"),
    (("星期", r"weeks?\b"), "weeks"), (("周", r"weeks?\b"), "weeks"), (("天", r"days?\b"), "days"),
    (("年", r"years?\b|decades?\b"), "years"),
]


def cn_number(s: str) -> float | None:
    """一千二百 -> 1200, 两万五千 -> 25000, 三点五 -> 3.5, 九千九百九十九 -> 9999; None if not a well-formed
    numeral."""
    if "点" in s:
        whole, _, frac = s.partition("点")
        w = cn_number(whole) if whole else 0
        if w is None or not frac or any(c not in _CN_DIGITS for c in frac):
            return None
        return w + float("0." + "".join(str(_CN_DIGITS[c]) for c in frac))
    total, section, digit = 0.0, 0.0, None
    for c in s:
        if c in _CN_DIGITS:
            if digit is not None and c not in "零〇":
                return None  # 三四: a range, not a number
            digit = _CN_DIGITS[c] if c not in "零〇" else None
        elif c in _CN_UNITS:
            section += (digit if digit is not None else 1) * _CN_UNITS[c]
            digit = None
        elif c in _CN_BIG:
            section += digit or 0
            if section == 0:
                return None
            total += section * _CN_BIG[c]
            section, digit = 0.0, None
        else:
            return None
    return total + section + (digit or 0)


def _cn_claims(text: str, problems: list[str] | None) -> list[NumberClaim]:
    out: list[NumberClaim] = []
    for m in _CN_NUM_RE.finditer(text):
        start, end = m.start(2), m.end()
        token, pct = m.group(2), bool(m.group(1))
        prev = text[max(0, m.start() - 1) : m.start()]
        if prev == "第" or any(text.startswith(w, start) for w in _CN_NOT_NUMBER):
            continue
        if re.match(r"[\d.,]", prev) or token[0] in "百千万亿":
            continue  # 9143亿元: the magnitude of an Arabic figure, read by number_claims
        tail = text[end : end + 3]
        if tail.startswith("国集团"):
            continue  # 二十国集团 / 七国集团: a name (11-01 r3 refused '二十国集团' as the figure 20)
        unit_word = next((c for c in _CN_CLASSIFIERS if tail.startswith(c)), None)
        if not (pct or unit_word):
            continue
        if text[max(0, m.start() - 1) : m.start()] in ("数", "几", "上") and not pct:
            continue  # part of a vague quantity
        value = cn_number(token)
        if value is None:
            if problems is not None:
                problems.append(f"cannot read the figure {m.group(0)}{unit_word or ''!s} -- write the source's figure")
            continue
        mag, unit, _ = _unit_after(text, end)
        if pct:
            unit = "pct"
        elif unit_word == "成":
            value, unit = value * 10, "pct"
        elif unit_word in ("%", "％"):
            unit = "pct"
        mag = 1.0 if token[-1] in _CN_BIG else mag  # 两万 already holds its magnitude
        article = value == 1 and token == "一" and unit_word != "人"  # 一名/一位/一项 = a; 一人 is a head count
        out.append(NumberClaim(value * mag, unit, 0.5 * mag if value == int(value) else 0.05 * mag, m.group(0),
                               article=article, what=_casualty_after(text, end)))
    return out


def _vague_claims(text: str) -> list[NumberClaim]:
    """数千 / thousands style quantities, longest Chinese form first (数百万 is millions, not hundreds)."""
    out, low, taken = [], text.lower(), []
    forms = sorted(((z, cls) for cls, zs, _ in _VAGUE for z in zs), key=lambda x: -len(x[0]))
    for z, cls in forms:
        for m in re.finditer(re.escape(z), text):
            if not any(a <= m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                out.append(NumberClaim(-1.0, f"vague:{cls}", 0.0, z))
    for cls, _zs, en_pat in _VAGUE:
        if re.search(en_pat, low):
            out.append(NumberClaim(-1.0, f"vague:{cls}", 0.0, cls))
    return out


def _en_word_claims(text: str, *, skip_one: bool) -> list[NumberClaim]:
    out = []
    for m in _EN_WORD_RE.finditer(text):
        w = m.group(1).lower()
        if skip_one and w == "one" and not m.group(2):
            continue
        value = _EN_WORDS[w] + (_EN_WORDS[m.group(2).lower()] if m.group(2) else 0)
        _mag, unit, _ = _unit_after(text, m.end())
        out.append(NumberClaim(float(value), unit, 0.5, m.group(0), what=_casualty_after(text, m.end())))
    return out


def numeric_claims(text: str, problems: list[str] | None = None, *, source: bool = False) -> list[NumberClaim]:
    """Every figure claim in `text`: Arabic numerals (number_claims), Chinese numerals with a measure word or unit,
    vague quantities (数千 / thousands), English number words. `source=True` reads a quote (every English number
    word counts); otherwise "one" alone is the article and is not a claim. Unreadable Chinese figures go to
    `problems`."""
    text = unicodedata.normalize("NFKC", text or "")
    return (number_claims(text) + _cn_claims(text, problems) + _vague_claims(text)
            + _en_word_claims(text, skip_one=not source))


def years_in(text: str) -> set[int]:
    """Years written as years (2026年, 2027财年, in 2026, the 1970s)."""
    text = unicodedata.normalize("NFKC", text or "")
    out = {int(m.group(0)) for m in re.finditer(r"(?<!\d)(19|20)\d{2}(?!\d)", text)
           if _year_context(text, m.start(), m.end())}
    out |= {int(m.group(1)) for m in re.finditer(r"\b((?:19|20)\d0)s\b", text)}
    return out


def number_claims(text: str) -> list[NumberClaim]:
    """Every Arabic-numeral figure in `text` that is not part of a date/time or of a name (SU7, Q3, GPT-5), with
    its scaled value, unit and the tolerance its own written precision allows."""
    text = unicodedata.normalize("NFKC", text or "")
    out: list[NumberClaim] = []
    for m in _NUM_RE.finditer(text):
        start, end = m.start(), m.end()
        if start > 0 and text[start - 1] in "-–" and start > 1 and text[start - 2].isalpha():
            continue  # GPT-5
        if _is_date_part(text, start, end):
            continue
        digits = m.group(1).replace(",", "")
        frac = m.group(2) or ""
        base = float(digits + frac)
        decimals = len(frac) - 1 if frac else 0
        mag, unit, _ = _unit_after(text, end)
        before = text[max(0, start - 4) : start]
        for sym, code in _CURRENCY_SYMBOLS:
            if before.rstrip().endswith(sym):
                unit = unit or code
                break
        step = 10 ** (-decimals) * mag
        out.append(NumberClaim(base * mag, unit, step / 2, m.group(0), what=_casualty_after(text, end)))
    return out


def _units_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None or a == b:
        return True
    return "/" in (a + b) and (a in b or b in a)  # ¥ is CNY or JPY


def _supported(n: NumberClaim, pool: list[NumberClaim], quote_text: str = "") -> bool:
    """Same unit class, and the source value rounds to what the output wrote -- never more than the old 1% relative
    tolerance (a coarser rounding such as 3.4亿 -> "3亿" is refused: quote the source's figure). A vague quantity
    (数千) needs the same vague class in the quotes (thousands / 数千); 一 as an article needs an article or a one."""
    if n.unit and n.unit.startswith("vague:"):
        return any(e.unit == n.unit for e in pool)
    if n.article and re.search(r"\b(a|an|one|single)\b|一", quote_text, re.IGNORECASE):
        return True
    if n.value == 1 and n.unit == "people" and re.search(r"\b(one|single)\b|一", quote_text, re.IGNORECASE):
        return True
    for e in pool:
        if e.unit and e.unit.startswith("vague:"):
            continue
        if n.what and e.what and n.what != e.what:
            continue  # the same figure, but the quote counts another kind of casualty
        diff = abs(n.value - e.value)
        if (_units_compatible(n.unit, e.unit) and diff <= max(n.tolerance, e.tolerance) + 1e-9
                and diff <= 0.01 * max(abs(e.value), 1e-9) + 1e-9):
            return True
    return False


def _dates_in(text: str) -> set[tuple[int, int]]:
    text = unicodedata.normalize("NFKC", text or "")
    found = {(int(mo), int(d)) for mo, d in re.findall(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)}
    for mon, d in re.findall(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", text,
                             re.IGNORECASE):
        found.add((_MONTHS[mon.lower()[:3]], int(d)))
    for d, mon in re.findall(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", text,
                             re.IGNORECASE):
        found.add((_MONTHS[mon.lower()[:3]], int(d)))
    return found


def _days_in(text: str) -> set[int]:
    text = unicodedata.normalize("NFKC", text or "")
    return {int(d) for d in re.findall(r"(?<![\d月])(\d{1,2})\s*[日号]", text)}


def _source_dates(evidence: EventInput) -> set[tuple[int, int]]:
    """Calendar dates the sources were published on, in UTC and in the brief's own zones (Beijing, Los Angeles)."""
    out: set[tuple[int, int]] = set()
    stamps = [s.get("published_at") for s in evidence.sources] + [evidence.reference_time]
    for raw in stamps:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        for off in (0, 8, -7, -8):
            d = dt.astimezone(timezone(timedelta(hours=off)))
            out.add((d.month, d.day))
    return out


def _source_years(evidence: EventInput) -> set[int]:
    out: set[int] = set()
    for raw in [s.get("published_at") for s in evidence.sources] + [evidence.reference_time]:
        try:
            out.add(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).year)
        except ValueError:
            continue
    return out


def _latin_names(text: str) -> list[str]:
    names = []
    for m in _LATIN_TOKEN_RE.finditer(unicodedata.normalize("NFKC", text or "")):
        tok = m.group(0).strip(" .-'’")
        if len(tok) < 2 or tok.lower() in _GENERIC_LATIN:
            continue
        names.append(tok)
    return names


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).lower()


def _name_supported(name: str, haystack: str) -> bool:
    """`name` or a reviewed alias occurs in `haystack`: Latin forms as whole words ("un" must not match inside
    "fundamental"), CJK forms as substrings (Chinese has no word spaces)."""
    plain, squeezed = _plain(haystack), _norm(haystack)
    original = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", haystack or ""))
    for v in alias_variants(name):
        v = _plain(v).strip()
        if not v:
            continue
        if re.search(r"[a-z0-9]", v) and not re.search(r"[一-鿿]", v):
            if len(v) <= 3 and v.isalpha():  # an acronym (US, UK, UN, WHO): capitals only, never the word "us"
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(v.upper()) + r"(?![A-Za-z0-9])", original):
                    return True
            elif re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", plain):
                return True
        elif _norm(v) in squeezed:
            return True
    return False


def _check_support(enrichment: EventEnrichment, evidence: EventInput, reasons: list[str]) -> None:
    by_id = {s["id"]: s for s in evidence.sources}
    source_texts = {sid: _norm(f"{s.get('title', '')}\n{s.get('excerpt', '')}") for sid, s in by_id.items()}
    allowed_dates = _source_dates(evidence)
    allowed_years = _source_years(evidence)
    # 2026-11-01 replay: "据BBC报道" names the offered source itself -- a publisher is attribution, not a new name
    publishers = " ".join(s.get("publisher", "") for s in evidence.sources)
    valid_quotes: dict[str, list[str]] = {}
    for group, fields in _FIELD_GROUPS.items():
        values = [(f, getattr(enrichment, f) or "") for f in fields]
        values = [(f, v) for f, v in values if v.strip()]
        if not values:
            continue
        quotes = enrichment.support.get(group) or []
        if not quotes:
            reasons.append(f"support[{group!r}] is empty: every written field needs verbatim source quotes")
            continue
        good_quotes: list[str] = []
        for q in quotes:
            qn = _norm(q.quote)
            if q.source_id not in by_id:
                reasons.append(f"support[{group!r}] cites source {q.source_id!r}, which was never offered")
            elif not (_quote_min(q.quote) <= len(q.quote.strip()) <= _QUOTE_MAX):
                reasons.append(
                    f"support[{group!r}] quote {q.quote[:40]!r} must be {_quote_min(q.quote)}-{_QUOTE_MAX} characters"
                )
            elif qn not in source_texts[q.source_id]:
                reasons.append(f"support[{group!r}] quote {q.quote[:60]!r} is not verbatim in source {q.source_id}")
            else:
                good_quotes.append(q.quote)
        if not good_quotes:
            continue
        valid_quotes[group] = good_quotes
        if group == "brief":  # the digest line only restates the item: it may rest on the item's own quotes
            good_quotes = good_quotes + valid_quotes.get("headline", []) + valid_quotes.get("what", [])
        quote_text = "\n".join(good_quotes)
        pool = numeric_claims(quote_text, source=True)
        quote_years = years_in(quote_text) | {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", quote_text)}
        quote_lower = quote_text.lower()
        prospective = [m for m in (*_UNCERTAINTY_MARKERS_EN, *_UNCERTAINTY_MARKERS_ZH)
                       if (m in quote_lower if m.isascii() else m in quote_text)]
        quote_dates = _dates_in(quote_text)
        quote_days = _days_in(quote_text) | {d for _, d in quote_dates}
        for field, value in values:
            if prospective and not _HEDGE_RE.search(value):
                reasons.append(
                    f"{field}: its quotes are prospective/unconfirmed ({', '.join(prospective[:3])}) but the text "
                    f"states it as settled -- keep the source's hedge (据报道/消息称/或将/计划, reportedly/could), or "
                    f"quote a span that states this claim without prospective wording"
                )
            unreadable: list[str] = []
            for n in numeric_claims(value, unreadable):
                if not _supported(n, pool, quote_text):
                    unit = f" {n.unit}" if n.unit else ""
                    reasons.append(
                        f"{field}: the figure {n.text!r} ({n.value:g}{unit}) is not in this field's quoted source "
                        f"text with the same value, unit and precision -- quote it exactly as the source gives it, "
                        f"or leave it out"
                    )
            reasons.extend(f"{field}: {u}" for u in unreadable)
            for y in years_in(value):
                if y not in quote_years and y not in allowed_years:
                    reasons.append(f"{field}: the year {y} is neither quoted nor a source's own year")
            for md in _dates_in(value):
                if md not in quote_dates and md not in allowed_dates:
                    reasons.append(f"{field}: the date {md[0]}月{md[1]}日 is neither quoted nor a source's own date")
            for day in _days_in(value) - {d for _, d in _dates_in(value)}:
                if day not in quote_days and day not in {d for _, d in allowed_dates}:
                    reasons.append(f"{field}: the day {day}日 is neither quoted nor a source's own date")
            if field in _ZH_FIELDS:
                for inner in _PAREN_RE.findall(value):
                    if re.search(r"[A-Za-z]", inner):
                        reasons.append(
                            f"{field}: remove the parenthesised Latin text ({inner}) -- do not add English names, "
                            f"translations or abbreviations after Chinese names"
                        )
                for name in _latin_names(_PAREN_RE.sub("", value)):
                    if not _name_supported(name, f"{quote_text}\n{publishers}"):
                        reasons.append(f"{field}: the name {name!r} is not in this field's quotes")


def _check_entities(enrichment: EventEnrichment, evidence: EventInput, reasons: list[str]) -> None:
    """Declared entities: each must be traceable to the offered sources (titles included) or the actor facts, either
    directly or through the curated alias table. A parenthesised part is checked on its own -- stripping the brackets
    never makes a new name inside them acceptable."""
    hay = (" ".join(f"{s.get('title', '')} {s.get('excerpt', '')} {s.get('publisher', '')}" for s in evidence.sources)
           + " " + evidence.candidate_title + " "
           + " ".join(f.text for f in evidence.facts if f.kind in ("actor", "target")))
    # 2026-11-01 (latest-edition run): a standard Chinese rendering (鲁比奥, 习近平, 霍尔木兹海峡) of a name the source
    # writes in Latin letters is accepted when the writer lists it with the source's exact spelling and that spelling
    # is in that source. Refusing it made the retry fall back to Latin names in Chinese text ("在Shenzhen会见Xi
    # Jinping"). Whether the rendering is the standard one is the brief verifier's check; every pair is recorded.
    by_id = {s["id"]: s for s in evidence.sources}
    rendered: set[str] = set()
    for r in enrichment.name_renderings:
        src = by_id.get(r.source_id)
        text = f"{src.get('title', '')} {src.get('excerpt', '')}" if src else ""
        if src is None or not r.source_name.strip() or not _name_supported(r.source_name, text):
            reasons.append(f"name rendering {r.zh!r} = {r.source_name!r}: that spelling is not in source "
                           f"{r.source_id!r}")
        else:
            rendered.add(_plain(r.zh).strip())
    for name in enrichment.entities:
        if _plain(name).strip() in rendered:
            continue
        # a Latin gloss in brackets is a second name and is checked on its own; a Chinese bracket is part of the name
        # (刚果（金）)
        glosses = [g for g in _PAREN_RE.findall(name) if re.search(r"[A-Za-z]", g)]
        head = name
        for g in glosses:
            head = re.sub(r"\s*[（(]" + re.escape(g) + r"[)）]", "", head)
        parts = [p.strip() for p in [head, *glosses] if p.strip()]
        for part in parts or [name]:
            if not _name_supported(part, hay):
                reasons.append(f"entity {part!r} is not traceable to any offered source (or a reviewed alias)")


def validate_enrichment(enrichment: EventEnrichment, evidence: EventInput) -> ValidationResult:
    reasons: list[str] = []
    is_zh = evidence.edition_id == "zh_cn"

    text_fields = [("what_happened", enrichment.what_happened), ("why_it_matters", enrichment.why_it_matters),
                   ("chinese_what_happened", enrichment.chinese_what_happened),
                   ("chinese_takeaway", enrichment.chinese_takeaway), ("chinese_headline", enrichment.chinese_headline),
                   ("english_headline", enrichment.english_headline),
                   ("chinese_brief_line", enrichment.chinese_brief_line)]

    # 0. The edition's own language must be written.
    required = ("chinese_headline", "chinese_what_happened") if is_zh else ("english_headline", "what_happened")
    for f in required:
        if not (getattr(enrichment, f) or "").strip():
            reasons.append(f"{f} is empty")

    # 1. Banned generic language and comments on what the sources lack, in every text field.
    for field_name, value in text_fields:
        for pattern in _BANNED_PATTERNS:
            if pattern.search(value or ""):
                reasons.append(f"{field_name} contains banned generic phrase: {pattern.pattern!r}")
        for pattern in _META_COMMENTARY:
            if pattern.search(value or ""):
                reasons.append(
                    f"{field_name} comments on what the sources do not say; state only what they report"
                )

    # 2. cited_fact_ids: optional now that every field carries quotes, but never a fabricated id.
    offered_ids = {f.id for f in evidence.facts}
    fabricated = set(enrichment.cited_fact_ids) - offered_ids
    if fabricated:
        reasons.append(f"cited_fact_ids references facts never offered: {sorted(fabricated)}")

    # 3. Claim grounding: verbatim quotes per field group; numbers (value, unit, precision), dates, Latin names and
    # parenthesised glosses checked against the field's own quotes.
    _check_support(enrichment, evidence, reasons)

    # 4. Declared entities.
    _check_entities(enrichment, evidence, reasons)

    # 5. Why line: only with a stated kind; a source judgment must be attributed in the sentence.
    why_text = (enrichment.chinese_takeaway if is_zh else enrichment.why_it_matters) or ""
    if why_text.strip():
        if enrichment.why_kind not in _WHY_KINDS:
            reasons.append(f"why line needs why_kind 'reported' or 'source_judgment', got {enrichment.why_kind!r}")
        elif enrichment.why_kind == "reported" and _INTERPRETIVE.search(why_text):
            reasons.append("the why line interprets (反映出/意味着/表明/shows/signals...): that is a source_judgment, "
                           "and the sentence must name whose judgment it is")
        elif enrichment.why_kind == "source_judgment" and not re.search(
            r"(认为|称|表示|指出|预计|分析|说|据|according to|said|says|expects|argues|analysts?)",
            why_text,
            re.IGNORECASE,
        ):
            reasons.append("a source_judgment why line must say whose judgment it is")
    elif enrichment.why_kind:
        reasons.append("why_kind is set but there is no why line")

    # 6. Uncertainty preservation: checked per field group in _check_support -- a field whose own quotes are
    # prospective or unconfirmed must keep a hedge. (Until 2026-11-01 ANY hedge word anywhere in the evidence forced a
    # hedge into the whole item, refusing e.g. a reported quarterly result because the article also said "could".)

    # 7. Category consistency.
    if evidence.category_candidates and enrichment.category not in evidence.category_candidates:
        reasons.append(
            f"category {enrichment.category!r} is not one of the deterministic candidates "
            f"{evidence.category_candidates}"
        )

    # 8. Complete sentences.
    for field_name, value in (("what_happened", enrichment.what_happened),
                              ("why_it_matters", enrichment.why_it_matters),
                              ("chinese_what_happened", enrichment.chinese_what_happened),
                              ("chinese_takeaway", enrichment.chinese_takeaway)):
        if not value or not value.strip():
            continue
        stripped = value.strip()
        if _ends_with_dangling_clause(stripped):
            reasons.append(f"{field_name} ends with a dangling/incomplete clause")
        tail = stripped[-4:-1] if len(stripped) > 3 else ""
        if stripped[-1] not in ".!?。！？" and not _is_abbreviation_period(tail):
            reasons.append(f"{field_name} does not end with terminal punctuation")

    # 9. Source attribution integrity.
    offered_source_ids = {s["id"] for s in evidence.sources}
    if not enrichment.supporting_source_ids:
        reasons.append("supporting_source_ids is empty")
    else:
        bad_sources = set(enrichment.supporting_source_ids) - offered_source_ids
        if bad_sources:
            reasons.append(f"supporting_source_ids references sources never offered: {sorted(bad_sources)}")

    return ValidationResult(ok=not reasons, reasons=reasons)
