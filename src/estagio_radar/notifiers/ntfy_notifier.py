"""Notificador mobile push instantâneo gratuito via ntfy.sh (API JSON UTF-8)."""

from __future__ import annotations

import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.notifiers.ntfy")


def _ascii_header(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).encode("ascii", errors="ignore").decode("ascii")


def _lisbon_hour(now: datetime | None = None) -> int:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    try:
        return ref.astimezone(ZoneInfo("Europe/Lisbon")).hour
    except Exception:
        return ref.astimezone(timezone.utc).hour


def _build_body_text(job: EnrichedJob) -> str:
    cp = job.company_profile
    tl = f"★ {cp.teamlyzer_rating:.1f}/5" if cp.teamlyzer_rating else "Sem rating"
    source_line = f"Fonte: {', '.join(job.sources)}"
    runner_label = (os.getenv("RADAR_RUNNER_LABEL") or "").strip()
    if runner_label:
        source_line += f" · [{runner_label}]"
    return (
        f"📍 {job.location} ({job.modality}) | ⏱ {job.freshness_label}\n"
        f"Teamlyzer: {tl} | {' '.join(job.badges[:4])}\n"
        f"{source_line}"
    )


def build_ntfy_headers_and_body(job: EnrichedJob) -> tuple[dict[str, str], str]:
    """Helper legado compatível com headers HTTP ASCII + corpo UTF-8."""
    priority = "4" if job.score >= 82 else "3"
    title = f"[{job.score:.0f}/100] {job.company}: {job.title}"
    headers = {
        "Title": _ascii_header(title)[:200],
        "Priority": priority,
        "Tags": "rocket,briefcase" if job.score >= 80 else "briefcase",
        "Click": job.job_url,
    }
    return headers, _build_body_text(job)


def build_ntfy_json_payload(
    job: EnrichedJob,
    *,
    topic: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Constrói payload JSON UTF-8 completo para ntfy.sh com botões e horário silencioso."""
    hour = _lisbon_hour(now)
    is_quiet = hour >= 23 or hour < 8
    if is_quiet and job.score < 90:
        priority = 2
    elif job.score >= 82:
        priority = 4
    else:
        priority = 3

    return {
        "topic": topic,
        "title": f"[{job.score:.0f}/100] {job.company}: {job.title}"[:200],
        "message": _build_body_text(job),
        "priority": priority,
        "tags": ["rocket", "briefcase"] if job.score >= 80 else ["briefcase"],
        "click": job.job_url,
        "actions": [
            {
                "action": "view",
                "label": "Candidatar",
                "url": job.job_url,
                "clear": False,
            }
        ],
    }


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
    """Envia uma única notificação UTF-8 arbitrária via ntfy.sh JSON API."""
    resolved_topic = (topic or os.getenv("NTFY_TOPIC") or "").strip().lstrip("/")
    resolved_server = (server or os.getenv("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    if not resolved_topic:
        return False

    prio_val = int(priority) if str(priority).strip().isdigit() else 3
    payload: dict[str, Any] = {
        "topic": resolved_topic,
        "title": title[:200],
        "message": body,
        "priority": prio_val,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
    }
    if click_url:
        payload["click"] = click_url

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(resolved_server, json=payload, timeout=10.0)
        if 200 <= res.status_code < 300:
            return True
        logger.warning("ntfy respondeu HTTP %s: %s", res.status_code, res.text[:200])
        return False
    except Exception as err:
        logger.warning("Erro ao enviar mensagem ntfy: %s", err)
        return False


async def send_ntfy_alerts(
    jobs: list[EnrichedJob],
    topic: str | None = None,
    server: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Envia alertas push UTF-8 via ntfy.sh JSON API com botão de candidatura direta."""
    resolved_topic = (topic or os.getenv("NTFY_TOPIC") or "").strip().lstrip("/")
    resolved_server = (server or os.getenv("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
    if not resolved_topic or not jobs:
        return False

    sent_count = 0
    had_error = False
    async with httpx.AsyncClient() as client:
        for job in jobs[:8]:
            payload = build_ntfy_json_payload(job, topic=resolved_topic, now=now)
            try:
                res = await client.post(resolved_server, json=payload, timeout=10.0)
                if 200 <= res.status_code < 300:
                    sent_count += 1
                else:
                    logger.warning("ntfy respondeu HTTP %s: %s", res.status_code, res.text[:200])
                    had_error = True
            except Exception as err:
                logger.warning("Erro ao enviar ntfy: %s", err)
                had_error = True
    return sent_count > 0 and not had_error
