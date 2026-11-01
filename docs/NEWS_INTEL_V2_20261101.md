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
