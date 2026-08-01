# Source policy

## Tiers

| Tier | Role | Can independently confirm? |
|---|---|---|
| T0 | Primary/official (government, regulator, company IR) | Yes, for what that entity did/said |
| T1 | High-reliability reporting (major wire, established newsroom) | Usually; policy still prefers T0 |
| T2 | Specialist reporting | Sometimes; needs corroboration for alerts |
| T3 | Discovery/heat (HN, Techmeme, GDELT, X mirrors) | No |

All sources are declared in `config/sources.yaml`, never hard-coded in
Python. Each entry has an `adapter`, `tier`, `poll_interval_seconds`, and
(for `html_index`) CSS `selectors`. Changing a source's endpoint or cadence
never requires a code change.

## Confirmation state machine

Implemented in `src/news_intel/pipeline/confirmation.py`, driven only by the
tiers/origin-families of sources actually attached to an event:

```
unverified -> reported   : any T1/T2 source attached
reported   -> confirmed  : >=2 distinct T1 origin_families attached
confirmed  -> official   : any T0 source attached (jumps straight there)
any        -> disputed   : sticky once set; never auto-cleared by this module
```

A single T3 source (HN, Techmeme, GDELT, a social mirror) can never move an
event past `unverified` on its own. Two outlets syndicating the same wire
copy share one `origin_family`, so they never count as two independent T1
corroborators — `raw_items.origin_family` tracks this explicitly.

## Social/X handles

`@TrumpDailyPosts`, `@FirstSquawk`, `@financialjuice` are declared in
`config/sources.yaml` as `social_stub` adapters, `enabled: false`. No
compliant free public endpoint was implemented or scraped — `SocialStubAdapter`
always reports `unavailable` with an explicit reason rather than pretending to
monitor them. This satisfies the design doc's requirement to never bypass
X's login/anti-bot controls or paid API.

## China sources

`gov_cn_policy` and `pboc_policy` are `html_index` adapters with CSS
selectors in config. They are isolated per-source: if a page's markup
changes, only that source degrades (see PRD.md limitations — the current
selectors need a live-page fix). GDELT queries provide English/Chinese
discovery fallback but per design never elevate an event to official status
on their own.
