# news-intel v2: the automatic Chinese brief (2026-11-01)

Branch `feature/news-intel-v2`. It merges `feature/news3min-quality` (bd364a2) and `feature/claude-provider` (b4bf28f)
and builds on them. Sections are added as the work lands; the summary and the deployment package come last.

## Sources

**Problem.** Neither edition had a general news pool. zh_cn collected IT之家 (1,633 items in the week to 11-01) and
量子位 (66), plus three official sites with a handful of items. en_global had BBC World as its only world feed. Within
an edition, not one event in the three replay windows had two independent source families.

**Probe (2026-11-01 ~06:50 UTC).** Each candidate feed was fetched once through this project's own `RssAdapter`. Its
robots.txt was parsed with `urllib.robotparser` for our User-Agent and the exact feed path.

| Feed | Result | Items | Newest | In last 24 h | Median excerpt | robots.txt |
|---|---|---|---|---|---|---|
| 中国新闻网 国内 `rss/china.xml` | **added** | 30 (span ~19 h) | 0.4 h | 30 | ~100 chars | allowed |
| 中国新闻网 国际 `rss/world.xml` | **added** | 30 (span ~24 h) | 0.2 h | 27 | ~80 | allowed |
| BBC 中文 `zhongwen/simp/rss.xml` | **added** | 42 | 2.4 h | 5 | ~60 | allowed |
| RFI 中文 `cn/rss` | **added** | 30 (span ~30 h) | 8.3 h | 19 | ~90 | allowed |
| 德国之声中文 `rss-chi-all` | **added** | 62 | 15.6 h | 5 | ~95 | allowed |
| The Guardian World | **added** | 45 | 0.5 h | 18 | ~600 | allowed |
| Al Jazeera all | **added** | 25 | 0.5 h | 25 | ~115 | allowed |
| France 24 English | **added** | 24 | 0.4 h | 22 | ~360 | allowed |
| SCMP China | **added** | 50 | 0.8 h | 12 | ~500 | allowed |
| 中国新闻网 滚动 `scroll-news.xml` | rejected | 30 | 0.0 h | 30 | ~106 | allowed; 30 items cover only ~1 h |
| 人民网 politics/world RSS | rejected | 100 | ~12,300 h | 0 | — | feed abandoned |
| 联合早报 realtime RSS | rejected | 0 | — | — | — | HTTP 404 |
| 纽约时报中文网 | rejected | 20 | 50 h | 0 | — | not updated |
| Nikkei Asia | rejected | 50 | — | — | — | no item dates |
| AP top news | rejected | 0 | — | — | — | feed host does not resolve |

The probe was taken on a Sunday morning (UTC), so the 24-hour counts are low for the weekday-heavy feeds.

**Edition membership.** Chinese-language feeds go to zh_cn and English feeds to en_global. The brief reads **both**
editions and merges the same event across them, so one feed is never collected twice. A source entry listing two
editions used to be accepted and silently ignored (collect.py used `editions[0]`, and `raw_items` is
`UNIQUE(source_id, external_id)`). It is now a configuration error.

**Effect on the existing dailies.** The new feeds enter the existing en/zh daily candidate pools too. This is listed
in the deployment package.

## Where general news was lost (three real windows, before this branch)

**Windows:** the 24 hours to 20:00 Beijing on 10-29, 10-30 and 10-31. Events were read from a copy of the
production database with the scores production had stored. Script: eval `scripts/funnel_quick.py`.

| Window to | Events | Unclassified | Qualified (≥58), zh / en | Events with ≥2 source families |
|---|---|---|---|---|
| 10-29 20:00 | 469 | 232 | 125 / 28 | 0 |
| 10-30 20:00 | 456 | 268 | 99 / 20 | 0 |
| 10-31 20:00 | 320 | 165 | 66 / 16 | 0 |

**Where the stories were lost, stage by stage:**
- **Collection:** the zh pool had no general news source.
- **Clustering:** events are clustered per edition, so BBC and IT之家 reports of one event never meet.
- **Classification:** general stories fall into Unclassified and are multiplied by 0.0 in the dailies, scoring
  about 39-45. Examples:
  - a deadly Spanish train crash;
  - a Russian drone strike that killed 13;
  - the US immigration-agents data leak.
- **Ranking:**
  - Dozens of single-source IT之家 items sat tied at 69.3-70.8.
  - In the production zh daily, the source cap then showed 2-4 of them.
  - The zh daily used the deterministic fallback every day.

## Generation, checking and retry

**The three recorded rejections, reproduced on the old validators with the real events** (`runs/repro-rejections-old.txt`):

| Case | Cause | Evidence |
|---|---|---|
| 国铁集团 (China State Railway Group) | Name equivalence: the model added an English gloss the source does not give, and there was no reviewed mapping. | Without the gloss it passes. |
| 5000 (Ebola) | Evidence omission: the figure was only in the BBC headline. The prompt and the validator never saw source titles. | Using the excerpt's 10,000 instead passes. |
| 32万亿日元 | Unit parsing: f97b73c read the source's "32 万亿日元" as a bare 32. | Fixed in bd364a2, which also closes a leak: "99万亿日元" passed unchecked before. |

**The retry never reached the model.** The rejection reasons were appended to `candidate_title`, and the prompt
never rendered that field. The retry prompt was byte-identical to the first (`runs/repro-retry-feedback-old.txt`).

**What changed:**
- **Evidence:**
  - Every source of the event, and of same-event clusters merged across editions, is offered: distinct
    families first, at most 6, each with its title, publisher, time, URL and language.
  - The model gets the cutoff as its reference time.
- **Prompt:**
  - Write the edition's language only; no English names in brackets.
  - Lead with what is new in the window.
  - A why line only when a source states a consequence ("reported") or a named source judges it
    ("source_judgment").
  - Verbatim quotes per field group.
  - An explicit RETRY block with the reasons; the sources stay unchanged.
- **Validator:** each field group's numbers must be in that group's own quotes, with the same unit or currency
  and the written precision.
  - It is never looser than the old 1% (3.4亿 → "3亿" is refused).
  - Numbers of 1 or less are no longer skipped.
  - Dates must be quoted or be a source's own date.
  - Latin names in Chinese text must be quoted; bracketed glosses are refused.
  - A bracketed part of a declared entity is checked on its own.
  - Comments on what the sources lack are refused.
  - An interpretive why line marked "reported" is refused.
  - Hedges are required where the quoted claim is unconfirmed.
- **Aliases:** every equivalence has a basis.
  - `config/entity_aliases.yaml`: pairs a collected source itself wrote (with the URL), the organisation's own
    Chinese site, or the 2026-08-25 replay.
  - `config/country_aliases.yaml`: generated from the UN's zh and en member-state lists, plus marked short forms.
  - Three-letter Latin aliases match capitals only, so "told us" is not the US.
- **Regex passing is not fact checking.** Each brief item is also checked by an independent verifier call against
  its own sources: relation, subject, period, sign, attribution, certainty, headline/body agreement, new today,
  and a supported why. The writer gets one rewrite with the verifier's problems. An item that still fails, or
  cannot be verified, is dropped.

**The same three cases with the new code and the real model** (`runs/repro-rejections-new.jsonl`, claude-opus-5-5):
- 国铁集团 zh, Ebola en and 存储五巨头 zh were validated on the first attempt (12 s, 10 s, 17 s).
- Ebola zh was refused for 刚果民主共和国 ↔ DR Congo, which had no mapping. It now resolves through the UN list.

## Claude provider, budget, dry-run, run status

- **Provider:** `ClaudeCliProvider` is the only caller; the dailies and the brief share it. Output is accepted
  only with positively confirmed identity:
  - `apiKeySource` is "none";
  - the init model and every model in `modelUsage` equal the requested one;
  - exit code 0, one turn, no tool or permission denial, a non-empty result.

  Each call's identity, duration and tokens are recorded. A failure takes the documented floor, never a paid API.
- **Budget:**
  - Measured Claude CLI calls on real evidence: 3-23 s.
  - No call starts with less than 20 s of the run budget left, and the call timeout shrinks to what is left.
  - The JSON-repair call is skipped near the deadline.
  - The dailies keep 150 s inside `TimeoutStartSec=280`, enough for 3-4 stories at this latency (zh dry run:
    1 story, 21 s).
  - The brief has its own budget, 1200 s inside `TimeoutStartSec=1800`; replays took 260-320 s.
- **Dry runs:**
  - `pi-news digest daily --dry-run` used to always say `skipped_lock`: the SAVEPOINT conflicted with
    BEGIN IMMEDIATE.
  - The lock is now nested when needed. Only real contention returns False; program errors raise.
  - CLI dry runs run on a private online-backup copy, so a minutes-long preview holds no production lock.
  - A live lock row still makes the preview stand aside.
  - Real CLI check on a copy of production: the zh dry run called claude-opus-5-5 and produced a brief.
- **Run status:**
  - `fallback_used` is true when any top story is on the floor.
  - `edition_status` is model / mixed / degraded / no_model / empty.
  - Every story's result, reason, retries and per-attempt latency are recorded, along with every CLI call's
    identity and tokens.

## The brief and its snapshot

`src/news_intel/brief/`, `jobs/brief.py`: see the module docstrings.

**Edition:** the Beijing edition date, cutoff 20:00 Asia/Shanghai, and the 24-hour window.

**Selection:**
- the pool is point-in-time, from both editions;
- a brief-only pre-rank: public safety on, Unclassified not zeroed, product and soft-news demotion, tier and
  family weight;
- a Claude editor call merges duplicates across languages and chooses 0-5 by importance with reasons, reserves
  and topic labels;
- earlier editions' headlines are shown, so a continuation must lead with what is new;
- there is a deterministic fallback.

**Items:** the editorial layer above, the "today" check, then the verifier.

**Snapshot `news-intel/brief-v1`:**
- written atomically and frozen;
- the same edition is reused;
- revisions are explicit;
- it records `text_sha256`, every source, the support quotes and the verification of each item, the selection
  reasons, rejections with stage and reason, the funnel, models, code/prompt/config hashes and every call.

**Outputs:** Telegram HTML, the dated archive page, and the newsletter digest. All three are rendered from the
frozen snapshot only.

## The newsletter digest (history-in-practice, branch `feature/news-digest-consumer`)

**Consumer only.** The story pipeline, recipients, SMTP, send idempotency and delivery records are untouched.

- **Check:** `news.py` checks the frozen brief:
  - its own hash;
  - the same Beijing edition date;
  - Asia/Shanghai, and a cutoff not in the future and at most 26 h old;
  - 1-5 items, each with a brief line and a link;
  - the archive page.
- **Binding:** the digest is bound once per edition revision, at the site step, and reused by every retry and
  manual send. The archive page goes into the same site commit.
- **Absent news:** anything wrong binds "absent" and the story goes out alone, byte-identical to today's email.
- **Tests:** 188 pass. The 9 CLI tests need `.venv` inside the checkout; they fail in any fresh worktree, on
  `main` too, and pass with it.

## Final evaluation run (code `4290921`)

**What ran:**
- Three replays on a fresh copy of the production database.
- Then the 2026-11-01 edition on the shadow collector's database: a production copy plus the 9 new feeds, collected
  from 06:53 UTC to the 12:00 UTC cutoff.
- One shared export directory, so each edition saw the earlier ones.
- Pinned in a detached worktree. Artefacts: `~/pi-news-intel-archive/eval/v2-20261101/final2/`.

| Edition | Items | 字 | Digest 字 | Time / calls | snapshot_id · text_sha256 |
|---|---|---|---|---|---|
| 10-29 | 5 | 746 | 230 | 150 s / 13 | brief-zh-2026-10-29-r1 · 938a459f… |
| 10-30 | **4** (short, not padded) | 789 | 197 | 360 s / 23 | brief-zh-2026-10-30-r1 · 37d82374… |
| 10-31 | 5 | 844 | 260 | 229 s / 15 | brief-zh-2026-10-31-r1 · 945c1528… |
| **11-01** | 5 | 717 | 237 | 214 s / 17 | brief-zh-2026-11-01-r1 · 3dd4b5c5bf741f1d618a590342f733c4b355472f925329dc273b83165327598d |

All four are `ready`, chosen by the editor model, written and verified by claude-opus-5-5 (Claude Code CLI 2.1.308,
subscription login).

**Why 10-30 has 4 items:**
- The verifier refused a Russian drone-strike headline that dropped the source's "officials say".
- The validator refused CXMT's "2690.24 亿元", which is not in the quote.
- The Israeli opposition item's digest line dropped "alleged".
- Two reserves were skipped as the same topic.

**The 11-01 edition (the latest):**
- **New feeds:** they made up a large share of the pool. 50 Al Jazeera, 43 中国新闻网 国内, 31 中国新闻网 国际,
  25 Guardian, 19 France 24, 18 RFI 中文, 12 SCMP, 6 DW 中文 and 4 BBC 中文 candidates.
- **Items:**
  - Ebola in DR Congo (the headline figure and the excerpt now agree);
  - the Iran-Qatar plan to reopen Hormuz;
  - the FBI attributing the Binance theft to North Korea (BBC, Al Jazeera and France 24 merged into one item,
    continuing 10-30);
  - Rubio on the Quad;
  - the G20 ministers in Atlanta.
- **Passed over by the editor:** follow-ups with no new development (the Oleshky reunions) and a live blog.

**Why there is an 11-01 r1 in `final/` as well.** That run is `f141213`, kept as the failure record. Its first
drafts used 鲁比奥 / 习近平 / 深圳 / 霍尔木兹海峡. The entity check refused them for lack of a mapping, and the retry
fell back to "在中国Shenzhen会见中国领导人Xi Jinping" and a vague "海峡". The `4290921` fix:
- the writer lists each Chinese rendering with the source's exact spelling, checked in that source;
- the verifier confirms it is the standard name;
- every pair is stored in the item (`name_renderings`).

**Consumer checked on real snapshots** (history-in-practice branch, 22:08 Beijing):

| Snapshot | Result |
|---|---|
| fresh | bound; digest 189 汉字; HTML 39.7 KB; .eml 95,969 B |
| hash-damaged | absent, "text does not match its text_sha256" |
| 50 h old | absent, "cutoff … is 50.1 h old (max 26 h)" |
| missing | absent; the email is byte-identical to today's |

**Retry:** a second `build` of a frozen edition returns `reused` with the same text_sha256, and the file stays
read-only.

**Previews** in `final2/preview/`:
- `archive-mobile.png`;
- `email-end.png`;
- `archive.html`;
- the full email `.html`, `.txt` and `.eml` (to a fictional recipient, never sent);
- Telegram text via `python -m news_intel.jobs.brief show`.

## Still open (not hidden)

- **Quality:**
  - **A wrong rendering got through.** 10-30 rendered Alphabet as "字母表公司". The verifier accepted it; the usual
    form is "谷歌母公司Alphabet". The renderings rest on one model checking another.
  - **Thin Chinese domestic news.** Collection only started for these feeds on 11-01, and the three replays
    predate it.
  - **Replays are not a strict as-of reconstruction** (see "What a replay can and cannot recover"). The new feeds
    are partly backfilled, not a full 24 h history.
- **Checking:**
  - **The verifier is the same model as the writer:** an independent call with an adversarial prompt, not an
    independent model. Relation and subject checks are model judgments, recorded per item.
  - **The dailies have no verifier.** For them, only the deterministic checks apply, name renderings included.
  - **Strictness costs items.** It refuses some important items (Russian drone strike, CXMT). The brief prefers
    fewer items to unchecked ones.
- **Operations:**
  - **One shared login.** `CLAUDE_CONFIG_DIR` is the history-in-practice login, which expires about 11-29. Its
    concurrent use with hip's nightly jobs was not stress-tested.
  - **Email size:** the HTML body is 36.3 KB with the digest, far below Gmail's ~102 KB HTML clip. The earlier
    comparison used the `.eml` size, which counts the text part and the transfer encoding.
  - **A clock-dependent test.** `tests/unit/test_cli_edition_wiring.py::test_production_weekly_reports_edition_on_late_catchup_skip`
    fails on a Sunday inside the weekly slot, on `main` too.
  - **The fallback floor.** For a zh story with only English sources it prints English text. The brief never shows
    the floor; the zh daily still can.

## Review fixes (2026-11-02)

**Source:** issues reproduced by ChatGPT on news-intel `bded4f4` and history-in-practice `a1eb527`.

| Issue | Fix | Commit |
|---|---|---|
| "地震造成2026人死亡" passed: any 1900-2099 figure was skipped as a year | Years only in a year context (2026年, 2027财年, in/since/fiscal 2026, "Oct 31, 2026", the 1970s), and they must be quoted or be a source's own year. Elsewhere the figure is checked as a count. | 8eec56c |
| "九千九百九十九人死亡" passed: Chinese numerals were never read | A Chinese numeral before a measure word or unit, or after 百分之, is a figure (两, 十余, 三成 = 30%, 百分之五, 三点五). An unreadable one (三四百人) is refused. Durations, vague quantities (数千 = thousands) and English number words are covered too. | 8eec56c |
| A true figure of another claim in the same quote (9,999 injured written as deaths) | A casualty figure keeps its class (dead / injured / infected / missing / arrested / evacuated), in zh and en | 37e8bc3 |
| A usage-limit failure froze an "empty" edition and returned success; the 20:50 retry reused it | Run outcomes: complete, no_qualifying_news, or technical. A technical failure freezes nothing before the last attempt: evidence goes to `attempts/`, exit 75, and the retry builds. The last attempt publishes the verified items as degraded (exit 3), or nothing (exit 1). | 7b65bca |
| freeze() published before rendering, so a crash left an edition without an archive page forever | Commit order: the read-only snapshot, then the renderings (read back), then `current.json`. A later run completes or repairs from the frozen file; the text hash never changes. Fault-injection tested. | 7b65bca |
| Relative time words (r2: "下月" from a 10-31 source) | Resolved against the source's own date, by both the writer and the verifier | 1dc708e |
| "二十国集团" read as the figure 20 | A numeral before 国集团 is a group name | eff2e35 |

**The latest brief now** (`docs/review/brief-2026-11-01/`):
- r3 is current: `83c2e2bc76e03d8c472c0a351ff0e8d88433e9512295075ce496f45384d01585`, code `1dc708e`, complete,
  5 items, 742 字, 13 calls, 194 s.
- r1 and r2 are kept beside it.
- The pack has the frozen JSON with the support quotes and checks, the Telegram text, the archive page, the digest,
  two previews, and the differences between revisions.

**Email size, corrected.** An earlier note compared the `.eml` size with Gmail's clip. The clip applies to the HTML
body.
- With main `2db35f5`'s layout, the 胡适 email's HTML is 36.3 KB with the digest and 35.1 KB without it.
- The `.eml` (text part and transfer encoding included) is 100.6 KB and 97.1 KB.

## Second review round (2026-11-02)

| Issue | Fix | Commit |
|---|---|---|
| A casualty class was read only after the figure, and an unknown class matched anything ("injured 9,999" supported "九千九百九十九人死亡"; "14, officials say" supported "14人死亡") | The class is read after or before the figure, and a stated class needs the same known class on the quoted figure | b64119c |
| 一名/一位 was "proved" by any a/an/一 in the quotes | 一人/一名/一位 is a head count of one: it needs one person bound to a person noun ("a businesswoman", "an official", 一名, 1人) | b64119c |
| 千人 / 百人 / 千万元 were skipped as magnitude-led | Read as figures; only a magnitude right after an Arabic figure (9143亿) is left to the Arabic reading | b64119c |
| Binance item: "据报道弥补比例为90%或95%" | `revise-item`: the model revised that item only, with the sources' timing and attribution as the instruction, fully re-checked; it left the percentages out. r4 is frozen; r3 and the other items are unchanged | 12ad330 |
| The 20:50 last attempt can end after a fixed 21:00 Telegram send | The send follows each successful build (OnSuccess drop-in, installed only at step G), is idempotent per edition, and refuses after cutoff + 3 h. The 21:00 timer is removed | this round |

**Real-data regression.** All 29 items of the frozen briefs were re-checked with the new validator: the three replays,
and 11-01 r1 to r3. Their evidence was rebuilt from the databases at each cutoff, and every package was identical to
the frozen evidence. All 29 pass. The first cut of the fix had refused three correct texts, fixed in b64119c:
- "近三十年来" for "three decades";
- "70年代" for "the 1970s";
- 一 with a non-person measure word (一次罕见的停火) read as a figure; it is the article.

## What a replay can and cannot recover

**A replay is not a strict historical as-of reconstruction.** The database keeps no history of edits, so these
replays approximate what a run at the cutoff would have seen. That does not rebuild the database; it only states
what the stored rows allow.

| Recoverable from stored rows | Not recoverable |
|---|---|
| Which raw items existed at the cutoff: `fetched_at`; later items are excluded. | The text of an item updated in place after the cutoff (CONTENT_CHANGED overwrites title, excerpt and hash). |
| Their publication time: `published_at`, used for the window. | Event membership before a post-cutoff reclustering (a changed item's link is deleted and re-made). |
| Event links of items whose content did not change. | Event-level fields stored later: `entities_json`, stored category and scores. The pre-rank recomputes the category and score from the items; it does not use the stored values. |
| Source and tier metadata. | Feed items the collector never stored, e.g. backfill of feeds added later. |

**How much the unrecoverable part touched these windows** (copy of 2026-11-01 06:49 UTC):

| Window | Candidate events | Events whose text changed after the cutoff | Of those, in the brief |
|---|---|---|---|
| 10-29 | 486 | 7 | 0 |
| 10-30 | 484 | 12 | 0 |
| 10-31 | 364 | 3 | 0 |

So no published replay item rests on text edited after its cutoff. Membership and entities of the other events
may still differ slightly from what a run at the cutoff would have stored.

**What a run at the real cutoff avoids:** these limits do not apply to the live 20:05 run, or to the 11-01 edition
built from the shadow database copied at the cutoff.

## Replays: three real windows, cutoff 20:00 Beijing

**Setup:**
- code pinned at `f141213` (a detached worktree, so later edits could not leak in);
- a fresh copy of the production database as of 2026-11-01 06:49 UTC;
- only items fetched by each cutoff are used: see "What a replay can and cannot recover" for the limits;
- the runs went in date order, so every edition saw the earlier ones;
- claude-opus-5-5 through the Claude Code CLI did all the writing;
- artefacts are in `~/pi-news-intel-archive/eval/v2-20261101/final/`: `out/` and `compare.json`.

**How these replays compare with the new feeds:** they predate the 9 new feeds, so they show what the old pool
could give once the selection and checking change. The pool is still English-world-heavy, and Chinese domestic
general news is thin.

| Edition | v2 brief (all automatic, all verified) | Production zh daily (same evening) | bd364a2 prototype pick |
|---|---|---|---|
| 10-29 | Spanish high-speed train crash (≥68 dead) · Israeli election deadlock · Ethiopia buys Russian jets · Microsoft cuts capex outlook · MOFCOM trade-in sales over 2 trillion yuan. 735 字, 176 s, 14 calls | 3 items, fallback: Microsoft's OpenAI revenue · a robotics lab · Alphabet's stock drop | Google ads · Microsoft buyouts · 佰维 earnings · Ethiopia · a PCB maker IPO |
| 10-30 | North Korea-linked Binance theft (TRM Labs) · Russian drone strike, ≥14 dead · DeepSeek raise · Alphabet results and 13% fall · Israeli opposition (continues 10-29). 812 字, 277 s, 20 calls | 2 items, fallback: Murata MLCC plan · TCL earnings | Intel Q3 · Google's Finland data centre · Luxshare earnings · Alphabet drop · TRM/Binance |
| 10-31 | Oleshky evacuation during a ceasefire · G20 photo cancelled · China denies the Nvidia-CEO spying claim · Intel's worst day since the 1970s · CXMT long-term supply talks. 776 字, 204 s, 15 calls | 2 items, fallback: the five memory makers' profits · CXMT supply talks | Intel ×2 · OpenAI raise · Geely remark · Binance |

**Short days and degraded runs:** none in these windows. Every edition had 5 verified items, and the editor
never fell back to the deterministic selection.

**Duplicates merged across languages:** the Intel crash merges IT之家 and CNBC; CXMT merges the en and zh
editions.

**Cross-day continuity:**
- On 10-31 the editor passed over the Binance recovery and Alphabet's further fall, both "already in the brief,
  no new development".
- On 10-30 the Israeli opposition item is marked as continuing 10-29's election item.

**Refused in the final run, and kept refused:** CXMT's "2690.24 亿元", a figure not in the quoted source text.

**False refusals found by the replays and fixed on this branch:**

| Refusal | Fixed in |
|---|---|
| 中国 / 美国 ↔ China / US | 903d0c0 |
| A Microsoft result refused because the article speculated elsewhere | 9435a14 |
| '知情人士称' rejected as too short; no way out of a hedged headline | 7aa325d |
| '据BBC报道' rejected; a digest line's name rejected | f141213 |

**Real problems the verifier caught and the rewrite fixed:**
- the brief line of the Oleshky item dropped "据了解";
- a G20 version turned "after protests" into a cause;
- the death-toll attribution "officials say" was lost in a headline.

**Length:** 735-812 字 per brief. Earlier settings show the trade-off: no floor gave 525-553 on two days, and
an 80-character floor made the model repeat facts. The final prompt asks for 100-160 characters of sourced
context per item.
