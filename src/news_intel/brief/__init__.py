"""The automatic Chinese daily news brief (news-intel v2, 2026-11-01).

One frozen, verified snapshot per Beijing edition date feeds every output: the full Telegram brief, the dated web
archive page and the newsletter's short digest (history-in-practice). Nothing here sends anything by itself.

  schedule  Beijing edition date, cutoff and 24-hour window
  pool      point-in-time candidates from both editions, deterministic pre-ranking, cross-day repeats
  select    the editor's choice: duplicates merged across languages, importance first (Claude), with a fallback
  compose   per-item text through pipeline/editorial_enrichment, then an independent verifier pass
  snapshot  versioned, hashed, atomically written and frozen JSON
  render    Telegram HTML, archive page, newsletter digest -- from the frozen snapshot only
"""
