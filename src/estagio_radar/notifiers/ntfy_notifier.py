"""Notificador mobile push instantâneo gratuito via ntfy.sh (sem conta necessária!)."""

from __future__ import annotations

import logging
import os

import httpx

from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.notifiers.ntfy")


def build_ntfy_headers_and_body(job: EnrichedJob) -> tuple[dict[str, str], str]:
    cp = job.company_profile
    tl = f"★ {cp.teamlyzer_rating:.1f}/5" if cp.teamlyzer_rating else "Sem rating"
    priority = "4" if job.score >= 82 else "3"
    title = f"[{job.score:.0f}/100] {job.company}: {job.title}"
    source_line = f"Fonte: {', '.join(job.sources)}"
    runner_label = (os.getenv("RADAR_RUNNER_LABEL") or "").strip()
    if runner_label:
        source_line += f" · [{runner_label}]"
    body = (
        f"📍 {job.location} ({job.modality}) | ⏱ {job.freshness_label}\n"
        f"Teamlyzer: {tl} | {' '.join(job.badges[:4])}\n"
        f"{source_line}"
    )
    headers = {
        "Title": title[:200].encode("latin-1", errors="ignore").decode("latin-1"),
        "Priority": priority,
        "Tags": "rocket,briefcase" if job.score >= 80 else "briefcase",
        "Click": job.job_url,
    }
    return headers, body


async def send_ntfy_message(
    title: str,
    body: str,
    *,
    click_url: str | None = None,
    priority: str = "3",
    tags: str = "rocket",
    topic: str | None = None,
    server: str | None = None,
) -> bool:
    """Envia uma única notificação UTF-8 arbitrária via ntfy.sh."""
    resolved_topic = (topic or os.getenv("NTFY_TOPIC") or "").strip().lstrip("/")
    resolved_server = (server or os.getenv("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    if not resolved_topic:
        return False

    endpoint = f"{resolved_server}/{resolved_topic}"
    headers = {
        "Title": title[:200].encode("latin-1", errors="ignore").decode("latin-1"),
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(endpoint, headers=headers, content=body.encode("utf-8"), timeout=10.0)
        if 200 <= res.status_code < 300:
            return True
        logger.warning("ntfy respondeu HTTP %s", res.status_code)
        return False
    except Exception as err:
        logger.warning("Erro ao enviar mensagem ntfy: %s", err)
        return False


async def send_ntfy_alerts(
    jobs: list[EnrichedJob],
    topic: str | None = None,
    server: str | None = None,
) -> bool:
    resolved_topic = (topic or os.getenv("NTFY_TOPIC") or "").strip().lstrip("/")
    resolved_server = (server or os.getenv("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    if not resolved_topic or not jobs:
        return False

    endpoint = f"{resolved_server}/{resolved_topic}"
    ok = True
    async with httpx.AsyncClient() as client:
        for job in jobs[:8]:
            headers, body = build_ntfy_headers_and_body(job)
            try:
                res = await client.post(endpoint, headers=headers, content=body.encode("utf-8"), timeout=10.0)
                if res.status_code >= 300:
                    logger.warning("ntfy respondeu HTTP %s", res.status_code)
                    ok = False
            except Exception as err:
                logger.warning("Erro ao enviar ntfy: %s", err)
                ok = False
    return ok
