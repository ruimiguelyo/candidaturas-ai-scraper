"""Scraper LinkedIn Jobs (Public Guest API) com ordenação sortBy=DD e frescura ao minuto."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any

import bs4
from curl_cffi.requests import AsyncSession

from estagio_radar.date_utils import parse_publication_datetime
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import ScraperBatchResult, clean_html_text

logger = logging.getLogger("estagio_radar.scrapers.linkedin")

LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"


def parse_linkedin_cards(html: str, default_location: str = "Portugal") -> list[RawJob]:
    """Extrai cards do HTML devolvido pelo endpoint guest do LinkedIn."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for card in soup.find_all("li"):
        title_tag = card.find("h3", class_="base-search-card__title")
        company_tag = card.find("h4", class_="base-search-card__subtitle")
        loc_tag = card.find("span", class_="job-search-card__location")
        link_tag = card.find("a", class_="base-card__full-link")
        date_tag = card.find("time")

        if not (title_tag and link_tag):
            continue

        title = title_tag.get_text(" ", strip=True)
        company = company_tag.get_text(" ", strip=True) if company_tag else "Confidencial"
        raw_loc = loc_tag.get_text(" ", strip=True) if loc_tag else default_location
        job_url = str(link_tag.get("href") or "").split("?")[0].strip()
        if not title or not job_url or job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        job_id = job_url.rstrip("/").split("-")[-1] if "-" in job_url else job_url

        # Preferir texto relativo se tiver horas/minutos ("20 minutes ago", "3 hours ago")!
        rel_text = date_tag.get_text(" ", strip=True) if date_tag else ""
        attr_dt = str(date_tag.get("datetime") or "").strip() if date_tag else ""
        if re.search(r"\b(?:minutes?|hours?|minutos?|horas?|just now|agora)\b", rel_text, re.I):
            pub_raw = rel_text
        else:
            pub_raw = attr_dt or rel_text
        pub_dt = parse_publication_datetime(pub_raw)

        low_loc = raw_loc.casefold()
        low_title = title.casefold()
        is_remote = "remote" in low_loc or "remoto" in low_loc or "remote" in low_title
        is_hybrid = "hybrid" in low_loc or "híbrido" in low_loc or "hibrido" in low_loc
        modality = "Remote" if is_remote else ("Hybrid" if is_hybrid else "On-site / Hybrid")

        jobs.append(
            RawJob(
                source="LinkedIn",
                source_type="linkedin",
                job_id=str(job_id),
                title=title,
                company=company,
                location=raw_loc,
                all_locations=[raw_loc],
                job_url=job_url,
                modality=modality,
                is_remote=is_remote,
                published_at=pub_dt,
                published_raw=pub_raw,
            )
        )
    return jobs


async def scrape_linkedin(
    queries: list[dict[str, Any]],
    *,
    time_window: str = "r604800",
    enrich_top_details: bool = False,
) -> ScraperBatchResult:
    """Executa pesquisas sequenciais no LinkedIn com pacing conservador contra HTTP 429."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = partial = failed = 0
    warnings: list[str] = []
    rate_limited = False

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,pt;q=0.8",
    }

    async with AsyncSession(impersonate="chrome120") as session:
        session.headers.update(headers)

        for q in queries:
            if rate_limited:
                break
            kw = str(q.get("keywords") or "")
            loc = str(q.get("location") or "Lisbon, Portugal")
            f_e = str(q.get("f_E") or "")
            total_wanted = int(q.get("total") or 15)
            max_pages = max(1, (total_wanted + 9) // 10)
            start = 0

            for page_idx in range(max_pages):
                params: dict[str, Any] = {
                    "keywords": kw,
                    "location": loc,
                    "f_TPR": time_window,
                    "sortBy": "DD",
                    "start": start,
                }
                if f_e:
                    params["f_E"] = f_e

                try:
                    res = await session.get(LINKEDIN_SEARCH_URL, params=params, timeout=15)
                    if res.status_code == 429:
                        await asyncio.sleep(5.0)
                        res = await session.get(LINKEDIN_SEARCH_URL, params=params, timeout=15)
                    if res.status_code == 429:
                        rate_limited = True
                        if page_idx > 0 or jobs:
                            partial += 1
                        else:
                            failed += 1
                        warnings.append(f"LinkedIn HTTP 429 na query '{kw}' — recolha parada graciosamente.")
                        break
                    if res.status_code != 200:
                        failed += 1
                        warnings.append(f"LinkedIn HTTP {res.status_code} para '{kw}'.")
                        break

                    parsed = parse_linkedin_cards(res.text, default_location=loc)
                    ok += 1
                    for j in parsed:
                        if j.job_url not in seen_urls:
                            seen_urls.add(j.job_url)
                            jobs.append(j)
                    if len(parsed) < 8:
                        break
                    start += 10
                except Exception as err:
                    failed += 1
                    warnings.append(f"LinkedIn '{kw}': {err}")
                    break

                await asyncio.sleep(random.uniform(1.6, 2.4))

            if not rate_limited:
                await asyncio.sleep(random.uniform(2.2, 3.2))

        # Enriquecimento limitado (máx. 6 detalhes) para vagas candidatas a estágio que precisem de descrição/nº candidatos
        if enrich_top_details and not rate_limited and jobs:
            targets = [
                j for j in jobs
                if any(
                    k in j.title.casefold()
                    for k in ("intern", "estág", "estag", "trainee", "graduate", "co-op", "beapro")
                )
            ][:6]
            for j in targets:
                if not j.job_id.isdigit():
                    continue
                try:
                    dres = await session.get(LINKEDIN_DETAIL_URL.format(job_id=j.job_id), timeout=12)
                    if dres.status_code == 429:
                        break
                    if dres.status_code == 200:
                        dsoup = bs4.BeautifulSoup(dres.text, "lxml")
                        desc_el = dsoup.select_one(".show-more-less-html__markup") or dsoup.select_one(".description__text")
                        if desc_el:
                            j.description = clean_html_text(str(desc_el), max_len=1500)
                        for crit in dsoup.select("li.description__job-criteria-item"):
                            hdr = crit.select_one("h3")
                            val = crit.select_one("span")
                            if hdr and val and "seniority" in hdr.get_text(" ", strip=True).casefold():
                                j.seniority = val.get_text(" ", strip=True)
                        app_el = dsoup.select_one(".num-applicants__caption") or dsoup.select_one("figcaption.num-applicants__caption")
                        if app_el:
                            m = re.search(r"(\d+)", app_el.get_text(" ", strip=True))
                            if m:
                                j.applicant_count = int(m.group(1))
                except Exception:
                    pass
                await asyncio.sleep(1.4)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="LinkedIn",
            category="linkedin",
            requests_ok=ok,
            requests_partial=partial,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )
