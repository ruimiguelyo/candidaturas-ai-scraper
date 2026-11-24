"""Scrapers remotos europeus/globais com pré-filtro geográfico rigoroso (Arbeitnow, Remotive, Himalayas)."""

from __future__ import annotations

import asyncio
import time

import httpx

from estagio_radar.date_utils import parse_publication_datetime
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import DEFAULT_HEADERS, ScraperBatchResult, clean_html_text, retry_async


async def scrape_arbeitnow(max_pages: int = 3) -> ScraperBatchResult:
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            try:
                res = await retry_async(
                    client.get,
                    "https://www.arbeitnow.com/api/job-board-api",
                    params={"page": str(page)},
                    timeout=12.0,
                )
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Arbeitnow p{page}: HTTP {res.status_code}")
                    break
                items = res.json().get("data") or []
                ok += 1
                if not items:
                    break
                for item in items:
                    title = str(item.get("title") or "").strip()
                    job_url = str(item.get("url") or "").strip()
                    if not title or not job_url:
                        continue
                    is_rem = bool(item.get("remote"))
                    loc = str(item.get("location") or ("Remote - Europe" if is_rem else "Europe"))
                    jobs.append(
                        RawJob(
                            source="Arbeitnow",
                            source_type="remote_eu",
                            job_id=str(item.get("slug") or job_url),
                            title=title,
                            company=str(item.get("company_name") or "N/A"),
                            location=loc,
                            all_locations=[loc],
                            job_url=job_url,
                            modality="Remote" if is_rem else "On-site / Hybrid",
                            is_remote=is_rem,
                            published_at=parse_publication_datetime(item.get("created_at")),
                            published_raw=str(item.get("created_at") or ""),
                            tags=[str(t) for t in (item.get("tags") or []) if t][:10],
                            description=clean_html_text(item.get("description")),
                        )
                    )
            except Exception as err:
                failed += 1
                warnings.append(f"Arbeitnow p{page}: {err}")
                break

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Arbeitnow",
            category="remote_eu",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_remotive() -> ScraperBatchResult:
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        try:
            res = await retry_async(
                client.get,
                "https://remotive.com/api/remote-jobs",
                params={"limit": "150"},
                timeout=12.0,
            )
            if res.status_code != 200:
                failed += 1
                warnings.append(f"Remotive: HTTP {res.status_code}")
            else:
                ok += 1
                for item in res.json().get("jobs") or []:
                    title = str(item.get("title") or "").strip()
                    job_url = str(item.get("url") or "").strip()
                    if not title or not job_url:
                        continue
                    loc = str(item.get("candidate_required_location") or "Worldwide")
                    tags = [str(t) for t in (item.get("tags") or []) if t]
                    if item.get("category"):
                        tags.append(str(item["category"]))
                    jobs.append(
                        RawJob(
                            source="Remotive",
                            source_type="remote_eu",
                            job_id=str(item.get("id") or job_url),
                            title=title,
                            company=str(item.get("company_name") or "N/A"),
                            location=loc,
                            all_locations=[loc],
                            job_url=job_url,
                            modality="Remote",
                            is_remote=True,
                            employment_type=str(item.get("job_type") or "") or None,
                            salary=str(item.get("salary") or "") or None,
                            published_at=parse_publication_datetime(item.get("publication_date")),
                            published_raw=str(item.get("publication_date") or ""),
                            tags=tags[:10],
                            description=clean_html_text(item.get("description")),
                        )
                    )
        except Exception as err:
            failed += 1
            warnings.append(f"Remotive: {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Remotive",
            category="remote_eu",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_himalayas(queries: list[str]) -> ScraperBatchResult:
    """Consulta Himalayas, capturando todas as restrições geográficas para filtragem estrita."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for q in queries:
            try:
                res = await retry_async(
                    client.get,
                    "https://himalayas.app/jobs/api/search",
                    params={"q": q, "limit": "50"},
                    timeout=12.0,
                )
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Himalayas '{q}': HTTP {res.status_code}")
                    continue
                ok += 1
                for item in res.json().get("jobs") or []:
                    title = str(item.get("title") or "").strip()
                    job_url = str(item.get("applicationLink") or item.get("guid") or "").strip()
                    if not title or not job_url or job_url in seen_urls:
                        continue
                    seen_urls.add(job_url)

                    locs = [str(x).strip() for x in (item.get("locationRestrictions") or []) if x]
                    if not locs:
                        loc_display = "Worldwide"
                    elif len(locs) > 40 and any("portugal" in l.casefold() for l in locs):
                        loc_display = "Worldwide (incl. Portugal)"
                    elif len(locs) > 4 and any("portugal" in l.casefold() for l in locs):
                        loc_display = f"Europe / Multi-country ({len(locs)} países, incl. Portugal)"
                    elif len(locs) > 4:
                        loc_display = f"{', '.join(locs[:3])} (+{len(locs) - 3})"
                    else:
                        loc_display = ", ".join(locs)
                    sen = item.get("seniority")
                    sen_str = ", ".join(str(x) for x in sen) if isinstance(sen, list) else (str(sen) if sen else None)
                    pub_raw = item.get("pubDate")

                    jobs.append(
                        RawJob(
                            source="Himalayas",
                            source_type="remote_eu",
                            job_id=str(item.get("guid") or job_url),
                            title=title,
                            company=str(item.get("companyName") or "N/A"),
                            location=loc_display,
                            all_locations=locs if locs else ["Worldwide"],
                            job_url=job_url,
                            modality="Remote",
                            is_remote=True,
                            employment_type=str(item.get("employmentType") or "") or None,
                            seniority=sen_str,
                            published_at=parse_publication_datetime(pub_raw),
                            published_raw=str(pub_raw or ""),
                            tags=[str(c) for c in (item.get("categories") or []) if c][:8],
                            description=clean_html_text(item.get("description") or item.get("excerpt")),
                        )
                    )
                await asyncio.sleep(0.25)
            except Exception as err:
                failed += 1
                warnings.append(f"Himalayas '{q}': {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Himalayas",
            category="remote_eu",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )
