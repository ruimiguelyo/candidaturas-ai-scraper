"""Scraper direto do quadro de vagas do Teamlyzer Portugal (pt.teamlyzer.com/companies/jobs)."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import bs4
import httpx

from estagio_radar.date_utils import parse_publication_datetime
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import DEFAULT_HEADERS, ScraperBatchResult, retry_async

TEAMLYZER_ORIGIN = "https://pt.teamlyzer.com"


def parse_teamlyzer_job_cards(html: str) -> list[RawJob]:
    """Extrai ofertas do HTML de pt.teamlyzer.com/companies/jobs incluindo rating da empresa."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for card in soup.select(".panel.jobcard"):
        title_link = card.select_one("a.jobcard__title-link")
        if not title_link:
            continue
        href = str(title_link.get("href") or "").strip()
        title = title_link.get_text(" ", strip=True)
        if not href or not title:
            continue

        job_url = href if href.startswith("http") else f"{TEAMLYZER_ORIGIN}{href}"
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        # Empresa e slug
        comp_link = card.select_one("a.jobcard__company")
        company = comp_link.get_text(" ", strip=True) if comp_link else "Empresa Teamlyzer"
        comp_href = str(comp_link.get("href") or "") if comp_link else ""
        comp_slug = comp_href.rstrip("/").split("/")[-1] if "/companies/" in comp_href else None

        # Hint de rating já incluído no card do Teamlyzer!
        rating_el = card.select_one(".jobcard__rating")
        reviews_el = card.select_one(".jobcard__reviews")
        rating_val = None
        reviews_val = 0
        if rating_el:
            m = re.search(r"([1-5](?:[.,]\d+)?)", rating_el.get_text(" ", strip=True))
            if m:
                rating_val = float(m.group(1).replace(",", "."))
        if reviews_el:
            m = re.search(r"(\d+)", reviews_el.get_text(" ", strip=True))
            if m:
                reviews_val = int(m.group(1))

        loc_el = card.select_one(".jobcard__location")
        raw_loc = loc_el.get_text(" ", strip=True) if loc_obj_exists(loc_el) else "Portugal"
        # Exemplo: "Porto (Regime: Híbrido)" ou "Lisboa (Regime: Remoto)"
        modality = "Unknown"
        is_remote = False
        if "remoto" in raw_loc.casefold() or "remote" in raw_loc.casefold():
            modality = "Remote"
            is_remote = True
        elif "híbrido" in raw_loc.casefold() or "hibrido" in raw_loc.casefold():
            modality = "Hybrid"
        elif "escritório" in raw_loc.casefold() or "presencial" in raw_loc.casefold():
            modality = "On-site"

        clean_loc = re.sub(r"\(Regime:[^)]*\)", "", raw_loc, flags=re.I).strip() or "Portugal"

        sen_el = card.select_one(".jobcard__seniority")
        seniority = sen_el.get_text(" ", strip=True) if sen_el else None

        date_el = card.select_one(".jobcard__on-site")
        pub_raw = ""
        if date_el:
            pub_raw = str(date_el.get("title") or date_el.get_text(" ", strip=True) or "").strip()
        if not pub_raw:
            card_txt = card.get_text(" ", strip=True).casefold()
            if re.search(r"\b(?:nova|novo|hoje)\b", card_txt):
                pub_raw = "hoje"

        tags = [t.get_text(" ", strip=True) for t in card.select(".jobcard__tag") if t.get_text(strip=True)]
        role_tag = card.select_one(".role-tag")
        if role_tag:
            rt = role_tag.get_text(" ", strip=True)
            if rt and rt not in tags:
                tags.insert(0, rt)

        job_id = job_url.rstrip("/").split("-")[-1] if "-" in job_url else job_url

        jobs.append(
            RawJob(
                source="Teamlyzer Jobs",
                source_type="pt_board",
                job_id=job_id,
                title=title,
                company=company,
                location=clean_loc,
                all_locations=[clean_loc, raw_loc],
                job_url=job_url,
                modality=modality,
                is_remote=is_remote,
                seniority=seniority,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
                tags=tags[:10],
                teamlyzer_hint=(
                    {"slug": comp_slug, "rating": rating_val, "reviews": reviews_val}
                    if comp_slug
                    else None
                ),
            )
        )
    return jobs


def loc_obj_exists(el: Any) -> bool:
    return el is not None


async def scrape_teamlyzer_jobs(entries: list[dict[str, str]]) -> ScraperBatchResult:
    """Recolhe vagas reais do job board português Teamlyzer."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for entry in entries:
            url = entry["url"]
            try:
                res = await retry_async(client.get, url, timeout=12.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Teamlyzer {entry.get('label', url)}: HTTP {res.status_code}")
                    continue
                parsed = parse_teamlyzer_job_cards(res.text)
                ok += 1
                for j in parsed:
                    if j.job_url not in seen_urls:
                        seen_urls.add(j.job_url)
                        jobs.append(j)
                await asyncio.sleep(0.35)
            except Exception as err:
                failed += 1
                warnings.append(f"Teamlyzer {entry.get('label', url)}: {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Teamlyzer Jobs",
            category="pt_board",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )
