"""Despachante multi-canal de alertas (ntfy, Discord, Telegram, Email SMTP)."""

from __future__ import annotations

import asyncio

from estagio_radar.models import EnrichedJob
from estagio_radar.notifiers.discord_notifier import send_discord_alerts
from estagio_radar.notifiers.email_notifier import send_email_alert
from estagio_radar.notifiers.ntfy_notifier import send_ntfy_alerts
from estagio_radar.notifiers.telegram_notifier import send_telegram_alerts


async def dispatch_notifications(
    jobs: list[EnrichedJob],
    *,
    new_only: bool = True,
) -> dict[str, bool]:
    """Envia alertas em paralelo para todos os canais configurados no ambiente."""
    if not jobs:
        return {}

    ordered = sorted(jobs, key=lambda j: (-j.score, j.company.casefold()))
    ntfy_res, discord_res, tg_res = await asyncio.gather(
        send_ntfy_alerts(ordered),
        send_discord_alerts(ordered),
        send_telegram_alerts(ordered),
    )
    email_res = await asyncio.to_thread(send_email_alert, ordered, new_only=new_only)

    return {
        "ntfy": ntfy_res,
        "discord": discord_res,
        "telegram": tg_res,
        "email": email_res,
    }
