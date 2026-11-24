"""Notificador instantâneo via Discord Webhook com embeds ricos."""

from __future__ import annotations

import logging
import os

import httpx

from estagio_radar.models import EnrichedJob, GeoZone

logger = logging.getLogger("estagio_radar.notifiers.discord")


def _embed_color(job: EnrichedJob) -> int:
    if job.geo_zone == GeoZone.SETUBAL_LOCAL:
        return 0x10B981  # Verde esmeralda (Setúbal!)
    if job.company_profile.company_tier == "S":
        return 0x6366F1  # Indigo (Tier S)
    if job.company_profile.is_consultancy:
        return 0xF59E0B  # Ambar (Consultora)
    return 0x0EA5E9  # Azul celeste


def build_discord_payload(jobs: list[EnrichedJob]) -> dict:
    embeds = []
    for job in jobs[:10]:
        cp = job.company_profile
        tl_str = (
            f"★ {cp.teamlyzer_rating:.1f}/5 ({cp.teamlyzer_reviews} rev.)"
            if cp.teamlyzer_rating
            else "Sem rating Teamlyzer"
        )
        if cp.avg_salary_eur:
            tl_str += f" · €{cp.avg_salary_eur // 1000}k méd."
        badges = " ".join(job.badges[:5])
        embeds.append({
            "title": f"[{job.score:.0f}/100] {job.title}",
            "url": job.job_url,
            "color": _embed_color(job),
            "description": f"**{job.company}** · {job.location} ({job.modality})\n{badges}",
            "fields": [
                {"name": "Teamlyzer", "value": tl_str, "inline": True},
                {"name": "Frescura", "value": job.freshness_label or "Recente", "inline": True},
                {"name": "Fonte", "value": ", ".join(job.sources), "inline": True},
            ],
            "footer": {"text": "Estágio Radar PT · Clica no título para candidatar já"},
        })
    return {
        "username": "Estágio Radar PT",
        "content": f"🚀 **{len(jobs)} nova(s) oportunidade(s) tech/IA detetada(s)!**",
        "embeds": embeds,
    }


async def send_discord_alerts(
    jobs: list[EnrichedJob],
    webhook_url: str | None = None,
) -> bool:
    url = (webhook_url or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not url or not jobs:
        return False

    success = True
    async with httpx.AsyncClient() as client:
        for i in range(0, min(len(jobs), 30), 10):
            batch = jobs[i : i + 10]
            try:
                res = await client.post(url, json=build_discord_payload(batch), timeout=10.0)
                if res.status_code >= 300:
                    logger.warning("Discord webhook respondeu HTTP %s", res.status_code)
                    success = False
            except Exception as err:
                logger.warning("Erro ao enviar Discord webhook: %s", err)
                success = False
    return success
