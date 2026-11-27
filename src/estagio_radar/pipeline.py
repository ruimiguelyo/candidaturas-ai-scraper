"""Orquestrador principal do Estágio Radar PT v4.0 (Sniper ao Minuto + Deep Sweep)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from estagio_radar.company_intel import CompanyIntelService, base_company_name, normalize_company_key
from estagio_radar.config_loader import load_companies_config, load_sources_config
from estagio_radar.date_utils import format_freshness
from estagio_radar.dedup import deduplicate_accepted
from estagio_radar.exporters import export_all_artifacts
from estagio_radar.filter_engine import evaluate_raw_job
from estagio_radar.models import EnrichedJob, FilterDecision, RawJob, SourceHealth
from estagio_radar.notifiers.dispatcher import dispatch_notifications
from estagio_radar.scoring import compute_score
from estagio_radar.scrapers.ats_boards import scrape_all_ats
from estagio_radar.scrapers.base import ETagCache, ScraperBatchResult
from estagio_radar.scrapers.eu_trackers import scrape_euraxess_pt, scrape_github_trackers
from estagio_radar.scrapers.linkedin import scrape_linkedin
from estagio_radar.scrapers.pt_portals import scrape_itjobs, scrape_net_empregos
from estagio_radar.scrapers.remote_eu import scrape_himalayas
from estagio_radar.scrapers.teamlyzer_jobs import scrape_teamlyzer_jobs
from estagio_radar.storage import RadarStorage

logger = logging.getLogger("estagio_radar.pipeline")

SOURCE_HEALTH_PREFIXES = {
    "Greenhouse ATS": ("Greenhouse",),
    "Ashby ATS": ("Ashby",),
    "Lever ATS": ("Lever",),
    "SmartRecruiters PT": ("SmartRecruiters",),
    "Recruitee ATS": ("Recruitee",),
    "Teamtailor RSS": ("Teamtailor",),
    "Workday CXS PT": ("Workday",),
    "Siemens Avature PT": ("Siemens Avature",),
    "SuccessFactors PT": ("SuccessFactors",),
    "Oracle HCM PT": ("OracleHCM",),
    "GitHub EU Trackers": ("EU Tracker",),
    "EURAXESS Portugal": ("EURAXESS Portugal",),
    "Teamlyzer Jobs": ("Teamlyzer Jobs",),
    "ITJobs.pt": ("ITJobs.pt",),
    "Net-Empregos": ("Net-Empregos",),
    "LinkedIn": ("LinkedIn",),
    "Himalayas": ("Himalayas",),
}


@dataclass
class ScanResult:
    active_jobs: list[EnrichedJob] = field(default_factory=list)
    new_jobs: list[EnrichedJob] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    source_health: list[SourceHealth] = field(default_factory=list)
    artifacts: dict[str, Path] = field(default_factory=dict)
    notifications_sent: dict[str, bool] = field(default_factory=dict)
    raw_count: int = 0
    duration_ms: int = 0


def _build_linkedin_queries(sources_cfg: dict[str, Any], mode: str) -> tuple[list[dict[str, Any]], str]:
    li = sources_cfg.get("linkedin") or {}
    if mode == "sniper":
        return list(li.get("sniper_queries") or []), "r3600"
    fast = list(li.get("fast_queries") or [])
    if mode == "fast":
        return fast, "r86400"
    return fast + list(li.get("full_extra_queries") or []), "r604800"


async def run_radar_scan(
    *,
    mode: str = "full",
    notify: bool = False,
    notify_all: bool = False,
    hide_consultancies: bool = False,
    include_all_portugal: bool = False,
    min_score: float = 0.0,
    db_path: str | Path = "data/radar.db",
    cache_path: str | Path = "data/company_cache.json",
    output_dir: str | Path = "output",
    console: Console | None = None,
    allow_teamlyzer_network: bool = True,
    etag_cache: ETagCache | None = None,
) -> ScanResult:
    """Executa recolha em modo sniper, fast ou full e atualiza SQLite + dashboard + alertas."""
    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()

    sources_cfg = load_sources_config()
    companies_cfg = load_companies_config()
    hard_excluded = tuple(companies_cfg.get("hard_excluded") or ["deloitte"])
    pure_tech_set = {normalize_company_key(x) for x in (companies_cfg.get("pure_tech_companies") or [])}
    recency_cfg = sources_cfg.get("recency") or {}

    if console:
        console.print(
            f"[bold cyan]>>> Estágio Radar PT v4.0 ({mode.upper()}) — "
            "A recolher ATS diretos, portais PT, Trackers EU e LinkedIn...[/bold cyan]"
        )

    li_queries, li_window = _build_linkedin_queries(sources_cfg, mode)
    tl_entries = list(sources_cfg.get("teamlyzer_jobs") or [])
    it_cfg = sources_cfg.get("itjobs") or {}
    it_queries = list(it_cfg.get("queries") or ["estagio", "intern"])

    if mode == "sniper":
        coros = [
            scrape_all_ats(sources_cfg, etag_cache=etag_cache, include_workday=False),
            scrape_teamlyzer_jobs([e for e in tl_entries if e.get("label") in {"internship", "recent_p1"}]),
            scrape_itjobs(["estagio", "intern"], max_pages=1),
            scrape_linkedin(li_queries, time_window=li_window, enrich_top_details=False),
        ]
    elif mode == "fast":
        coros = [
            scrape_all_ats(sources_cfg, etag_cache=etag_cache),
            scrape_github_trackers(list(sources_cfg.get("github_trackers") or [])),
            scrape_teamlyzer_jobs(tl_entries[:4]),
            scrape_itjobs(it_queries, max_pages=1),
            scrape_linkedin(li_queries, time_window=li_window, enrich_top_details=False),
        ]
    else:
        rem_cfg = sources_cfg.get("remote_eu") or {}
        coros = [
            scrape_all_ats(sources_cfg, etag_cache=etag_cache),
            scrape_github_trackers(list(sources_cfg.get("github_trackers") or [])),
            scrape_euraxess_pt(list(sources_cfg.get("euraxess_pt") or [])),
            scrape_teamlyzer_jobs(tl_entries),
            scrape_itjobs(it_queries, max_pages=int(it_cfg.get("max_pages", 2))),
            scrape_net_empregos(list((sources_cfg.get("net_empregos") or {}).get("searches") or [])),
            scrape_linkedin(li_queries, time_window=li_window, enrich_top_details=False),
            scrape_himalayas(list(rem_cfg.get("himalayas_queries") or ["AI Intern", "Software Engineer Intern"])),
        ]

    gathered = await asyncio.gather(*coros, return_exceptions=True)

    raw_jobs: list[RawJob] = []
    health_list: list[SourceHealth] = []

    for item in gathered:
        if isinstance(item, Exception):
            logger.warning("Erro num scraper: %s", item)
            continue
        if isinstance(item, list):
            for sub in item:
                if isinstance(sub, ScraperBatchResult):
                    raw_jobs.extend(sub.jobs)
                    health_list.append(sub.health)
        elif isinstance(item, ScraperBatchResult):
            raw_jobs.extend(item.jobs)
            health_list.append(item.health)

    accepted_pairs: list[tuple[RawJob, FilterDecision]] = []
    rejections: list[dict[str, Any]] = []
    accepted_by_source: Counter[str] = Counter()

    for job in raw_jobs:
        if job.source_type == "ats":
            r_days = int(recency_cfg.get("ats_days", 60))
        elif job.source_type == "linkedin":
            r_days = int(recency_cfg.get("linkedin_days", 7))
        else:
            r_days = int(recency_cfg.get("board_days", 14))

        comp_brand_key = normalize_company_key(base_company_name(job.company))
        dec = evaluate_raw_job(
            job,
            hard_excluded=hard_excluded,
            recency_days=r_days,
            now=started_at,
            include_all_portugal=include_all_portugal,
            extra_tech_company_override=(comp_brand_key in pure_tech_set),
        )
        if dec.accepted:
            accepted_pairs.append((job, dec))
            accepted_by_source[job.source] += 1
        else:
            rejections.append({
                "source": job.source,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "reason_code": dec.reason_code,
                "reason_detail": dec.reason_detail,
                "job_url": job.job_url,
            })

    for h in health_list:
        prefixes = SOURCE_HEALTH_PREFIXES.get(h.source, (h.source.split()[0],))
        h.accepted_count = sum(
            cnt for src_name, cnt in accepted_by_source.items()
            if any(src_name.startswith(p) for p in prefixes)
        )

    merged_items = deduplicate_accepted(accepted_pairs)

    intel = CompanyIntelService(cache_path=cache_path)
    profiles = await intel.resolve_companies(
        [m["job"] for m in merged_items],
        allow_network=allow_teamlyzer_network,
    )

    enriched_jobs: list[EnrichedJob] = []
    for item in merged_items:
        job: RawJob = item["job"]
        comp_key = normalize_company_key(job.company)
        profile = profiles.get(comp_key) or intel._build_profile(job.company, None, job.teamlyzer_hint)
        if hide_consultancies and profile.is_consultancy:
            continue

        score, breakdown, badges = compute_score(
            role_tier=item["role_tier"],
            domain=item["domain"],
            geo_zone=item["geo_zone"],
            company=profile,
            published_at=job.published_at,
            applicant_count=job.applicant_count,
            now=started_at,
            primary_source=job.source,
            source_type=job.source_type,
        )
        if score < min_score:
            continue

        pub_dt = job.published_at
        enriched_jobs.append(
            EnrichedJob(
                fingerprint=item["fingerprint"],
                title=job.title,
                company=job.company,
                location=job.location,
                job_url=job.job_url,
                canonical_url=item["canonical_url"],
                sources=item["sources"],
                primary_source=job.source,
                source_type=job.source_type,
                modality=job.modality,
                is_remote=job.is_remote,
                role_tier=item["role_tier"],
                domain=item["domain"],
                geo_zone=item["geo_zone"],
                salary=job.salary,
                published_at=pub_dt,
                published_date=pub_dt.date().isoformat() if pub_dt else "",
                freshness_label=format_freshness(pub_dt, started_at),
                tags=job.tags,
                description_snippet=(job.description or "")[:600],
                applicant_count=job.applicant_count,
                company_profile=profile,
                score=score,
                score_breakdown=breakdown,
                badges=badges,
            )
        )

    enriched_jobs.sort(key=lambda j: (-j.score, j.company.casefold(), j.title.casefold()))

    storage = RadarStorage(db_path=db_path)
    all_seen_jobs, new_jobs = storage.upsert_jobs(
        enriched_jobs,
        deactivate_stale_full=(mode == "full"),
        now=started_at,
    )
    active_jobs = storage.list_jobs(
        active_only=True,
        hide_consultancies=hide_consultancies,
        min_score=min_score,
    )

    finished_at = datetime.now(timezone.utc)
    duration_ms = int((time.monotonic() - t0) * 1000)
    storage.record_run(
        started_at=started_at,
        finished_at=finished_at,
        mode=mode,
        raw_count=len(raw_jobs),
        accepted_count=len(all_seen_jobs),
        new_count=len(new_jobs),
        duration_ms=duration_ms,
        source_health=health_list,
    )

    artifacts = export_all_artifacts(active_jobs, rejections, output_dir=output_dir)

    sent_map: dict[str, bool] = {}
    if notify_all and active_jobs:
        sent_map = await dispatch_notifications(active_jobs, new_only=False)
        if any(sent_map.values()):
            storage.mark_notified([j.fingerprint for j in active_jobs], now=finished_at)
    elif notify and new_jobs:
        skip_consultancies = os.getenv("NOTIFY_SKIP_CONSULTANCIES", "").strip().lower() in {"1", "true", "yes"}
        targets = new_jobs
        if skip_consultancies:
            skipped = [j for j in new_jobs if j.company_profile.is_consultancy]
            targets = [j for j in new_jobs if not j.company_profile.is_consultancy]
            if skipped:
                storage.mark_notified([j.fingerprint for j in skipped], now=finished_at)
        if targets:
            sent_map = await dispatch_notifications(targets, new_only=True)
            if any(sent_map.values()):
                storage.mark_notified([j.fingerprint for j in targets], now=finished_at)

    if console:
        _print_summary(console, health_list, active_jobs, new_jobs, rejections, artifacts, sent_map, duration_ms)

    return ScanResult(
        active_jobs=active_jobs,
        new_jobs=new_jobs,
        rejections=rejections,
        source_health=health_list,
        artifacts=artifacts,
        notifications_sent=sent_map,
        raw_count=len(raw_jobs),
        duration_ms=duration_ms,
    )


def _print_summary(
    console: Console,
    health: list[SourceHealth],
    active_jobs: list[EnrichedJob],
    new_jobs: list[EnrichedJob],
    rejections: list[dict[str, Any]],
    artifacts: dict[str, Path],
    sent_map: dict[str, bool],
    duration_ms: int,
) -> None:
    ht = Table(title=f"SAÚDE DAS FONTES ({duration_ms / 1000:.1f}s)", show_lines=False)
    ht.add_column("Fonte")
    ht.add_column("OK", justify="right")
    ht.add_column("Falhas", justify="right")
    ht.add_column("Brutas", justify="right")
    ht.add_column("Qualificadas", justify="right")
    ht.add_column("Tempo (ms)", justify="right")
    for h in health:
        ht.add_row(h.source, str(h.requests_ok), str(h.requests_failed), str(h.raw_count), str(h.accepted_count), str(h.duration_ms))
    console.print(ht)

    jt = Table(title=f"TOP OPORTUNIDADES ({len(active_jobs)} ATIVAS | {len(new_jobs)} NOVAS)", show_lines=True)
    jt.add_column("Score", style="bold green", width=6)
    jt.add_column("Empresa", style="cyan", width=20)
    jt.add_column("Teamlyzer", style="yellow", width=15)
    jt.add_column("Cargo", style="bold white", width=34)
    jt.add_column("Zona / Local", style="magenta", width=22)
    jt.add_column("Frescura", width=9)
    jt.add_column("Fonte", width=20)

    for j in active_jobs[:35]:
        cp = j.company_profile
        tl = f"★{cp.teamlyzer_rating:.1f} ({cp.teamlyzer_reviews})" if cp.teamlyzer_rating else "-"
        if cp.is_consultancy:
            tl = f"⚠ {tl}"
        jt.add_row(
            f"{j.score:.0f}",
            j.company,
            tl,
            j.title,
            f"{j.geo_zone.value.replace('_LOCAL','').replace('_METRO','')} · {j.location}",
            j.freshness_label,
            ", ".join(j.sources),
        )
    console.print(jt)

    rc = Counter(r["reason_code"] for r in rejections)
    if rc:
        rt = Table(title="AUDITORIA DE REJEIÇÕES", show_lines=False)
        rt.add_column("Motivo")
        rt.add_column("Qtd", justify="right")
        for code, count in sorted(rc.items(), key=lambda x: -x[1]):
            rt.add_row(code, str(count))
        console.print(rt)

    console.print(f"[bold green]Dashboard HTML:[/bold green] {artifacts.get('dashboard')}")
    console.print(f"[bold green]JSON / CSV:[/bold green] {artifacts.get('json')} | {artifacts.get('csv')}")
    if sent_map:
        console.print(f"[bold cyan]Alertas enviados:[/bold cyan] {sent_map}")
