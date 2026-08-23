"""Explicit, mandatory capability required for any production entrypoint to
actually contact Telegram (2026-08-23 repair).

Real incident: `run_weekly_production()` resolved a real chat_id from config
and called `run_weekly(..., dry_run=False, ...)` unconditionally whenever its
Sunday-window check passed -- there was no way to construct or preview a
production digest without also being one config-with-real-credentials away
from a genuine send. During this incident's own verification work, calling
`run_weekly_production()` directly to test the Sunday-window gate returned
safely ONLY because the isolated worktree had no `.env` file, so
`config.secrets.allowed_chat_id_list` resolved empty and the `len(allowed)
!= 1` fail-closed branch caught it -- missing credentials, not a deliberate
safety mechanism, is what prevented a real send that time. The existing test
suite has the identical latent gap: every `run_daily(..., chat_id=555,
dry_run=False, ...)` test only avoids a real network call because the test
`config` fixture deletes `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_CHAT_IDS`
from the environment, not because chat_id=555 is structurally blocked.

This module's contract: a production entrypoint (jobs/daily_production.py,
jobs/weekly_production.py, jobs/monthly_production.py) accepts an
`authorization: ProductionDeliveryAuthorization | None = None` parameter.
With it omitted (the default everywhere except the CLI's actual
`production daily/weekly/monthly` commands -- the only real, systemd-
invoked, scheduled entrypoints), the function ALWAYS constructs/renders the
digest and returns it, but never resolves a real chat_id, never constructs
a TelegramSender, and never calls anything network-capable -- independent
of what secrets happen to be present in the environment. Constructing this
object is the one deliberate, explicit "yes, this is a real scheduled
production run" action; nothing else in the codebase should construct it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductionDeliveryAuthorization:
    """A capability marker, not a credential -- it carries no secret and
    grants nothing by itself; it only satisfies production entrypoints'
    required-parameter check that a real send was deliberately intended.
    Never construct this outside the CLI's `production daily/weekly/monthly`
    commands. Never invoke a `*_production` entrypoint directly (with or
    without this) for validation/replay/testing -- use the lower-level
    run_daily()/run_weekly()/run_monthly() with an explicit chat_id=None
    instead, which is structurally non-delivering regardless of this
    module."""


PRODUCTION_DELIVERY_AUTHORIZED = ProductionDeliveryAuthorization()
