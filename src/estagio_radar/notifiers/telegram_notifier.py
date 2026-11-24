"""Notificador instantâneo via Telegram Bot API."""

from __future__ import annotations

import logging
import os
from html import escape

import httpx

from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.notifiers.telegram")


def format_telegram_message(jobs: list[EnrichedJob]) -> str:
    lines = [f"<b>🚀 Estágio Radar PT — {len(jobs)} Nova(s) Oportunidade(s)!</b>\n"]
    for idx, job in enumerate(jobs[:12], 1):
        cp = job.company_profile
        tl = f"★ {cp.teamlyzer_rating:.1f}/5" if cp.teamlyzer_rating else "Sem rating"
        badges = " ".join(escape(b) for b in job.badges[:4])
        lines.append(
            f"{idx}. <b>[{job.score:.0f}] {escape(job.title)}</b>\n"
            f"   🏢 {escape(job.company)} ({tl}) · 📍 {escape(job.location)} · ⏱ {escape(job.freshness_label)}\n"
            f"   {badges}\n"
            f"   👉 <a href=\"{escape(job.job_url, quote=True)}\">Candidatar agora</a>\n"
        )
    if len(jobs) > 12:
        lines.append(f"<i>...e mais {len(jobs) - 12} vaga(s) no dashboard.</i>")
    return "\n".join(lines)


async def send_telegram_alerts(
    jobs: list[EnrichedJob],
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    cid = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not cid or not jobs:
        return False

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": format_telegram_message(jobs),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(api_url, json=payload, timeout=10.0)
            if res.status_code >= 300:
                logger.warning("Telegram respondeu HTTP %s: %s", res.status_code, res.text[:200])
                return False
        return True
    except Exception as err:
        logger.warning("Erro ao enviar Telegram: %s", err)
        return False
