"""Scrapers dos portais portugueses: ITJobs.pt, Landing.jobs API v1 e Net-Empregos."""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

import bs4
import httpx
from curl_cffi.requests import AsyncSession

from estagio_radar.date_utils import parse_publication_datetime
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import DEFAULT_HEADERS, ScraperBatchResult, clean_html_text, retry_async

ITJOBS_BASE = "https://www.itjobs.pt"
ITJOBS_SEARCH = f"{ITJOBS_BASE}/emprego"
LANDING_API_V1 = "https://landing.jobs/api/v1/jobs"
NET_EMPREGOS_SEARCH = "https://www.net-empregos.com/pesquisa-empregos.asp"

PT_CITIES = (
    ("setúbal", "Setúbal, Portugal"),
    ("setubal", "Setúbal, Portugal"),
    ("palmela", "Palmela, Setúbal, Portugal"),
    ("almada", "Almada, Setúbal, Portugal"),
    ("seixal", "Seixal, Setúbal, Portugal"),
    ("barreiro", "Barreiro, Setúbal, Portugal"),
    ("montijo", "Montijo, Setúbal, Portugal"),
    ("alcochete", "Alcochete, Setúbal, Portugal"),
    ("oeiras", "Oeiras, Lisboa, Portugal"),
    ("cascais", "Cascais, Lisboa, Portugal"),
    ("sintra", "Sintra, Lisboa, Portugal"),
    ("amadora", "Amadora, Lisboa, Portugal"),
    ("loures", "Loures, Lisboa, Portugal"),
    ("lisboa", "Lisboa, Portugal"),
    ("lisbon", "Lisbon, Portugal"),
    ("porto", "Porto, Portugal"),
    ("braga", "Braga, Portugal"),
    ("aveiro", "Aveiro, Portugal"),
    ("coimbra", "Coimbra, Portugal"),
    ("faro", "Faro, Portugal"),
)


def parse_itjobs_html(html: str) -> list[RawJob]:
    soup = bs4.BeautifulSoup(html or "", "lxml")
    jobs: list[RawJob] = []
    seen_ids: set[str] = set()

    links = soup.find_all("a", class_="title", href=lambda h: h and "/oferta/" in h)
    if not links:
        links = soup.find_all("a", href=lambda h: h and "/oferta/" in h)

    for link in links:
        href = str(link.get("href") or "").strip()
        parts = href.strip("/").split("/")
        job_id = parts[1] if len(parts) >= 2 and parts[1].isdigit() else re.sub(r"[^a-z0-9]", "", href)
        if not job_id or job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        title = link.get_text(" ", strip=True).replace("\xa0", " ")
        if not title or len(title) < 3:
            continue

        parent = (
            link.find_parent("li")
            or link.find_parent("div", class_="details")
            or link.find_parent("article")
            or link.find_parent("div")
        )

        company = "Consultar Anúncio"
        loc_name = "Portugal"
        date_str: str | None = None
        salary: str | None = None
        is_remote = False
        modality = "On-site / Hybrid"

        if parent:
            list_name_el = parent.select_one(".list-name a") or parent.select_one(".list-name")
            comp_el = list_name_el or parent.find("a", href=lambda h: h and "/empresa/" in h)
            if comp_el:
                if comp_el.get("title"):
                    company = str(comp_el["title"]).strip()
                elif comp_el.find("img") and comp_el.find("img").get("alt"):
                    company = str(comp_el.find("img")["alt"]).strip()
                elif comp_el.get_text(strip=True):
                    company = comp_el.get_text(strip=True)

            parent_text = parent.get_text(" ", strip=True).replace("\xa0", " ")
            low_text = parent_text.casefold()
            listing = parent.find_parent("ul", class_="listing")
            heading = listing.find_previous_sibling("div", class_="heading") if listing else None
            date_text = heading.get_text(" ", strip=True) if heading else parent_text

            if "remoto" in low_text or "remote" in low_text:
                is_remote = True
                modality = "Remote"
            elif "híbrido" in low_text or "hibrido" in low_text:
                modality = "Hybrid"

            matched_locs = [formatted for token, formatted in PT_CITIES if token in low_text]
            if matched_locs:
                loc_name = matched_locs[0]
            elif is_remote:
                loc_name = "Portugal (Remote)"

            dm = re.search(
                r"\b(hoje|ontem|\d{1,2}\s*(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez))\b",
                date_text,
                re.I,
            )
            if dm:
                date_str = dm.group(1)

            sm = re.search(r"([€$]\s*[\d\s\.]+(?:\s*-\s*[€$]?\s*[\d\s\.]+)?)", parent_text)
            if sm:
                salary = sm.group(1).strip()

        full_url = f"{ITJOBS_BASE}{href}" if href.startswith("/") else href
        jobs.append(
            RawJob(
                source="ITJobs.pt",
                source_type="pt_board",
                job_id=job_id,
                title=title,
                company=company,
                location=loc_name,
                all_locations=[loc_name],
                job_url=full_url,
                modality=modality,
                is_remote=is_remote,
                salary=salary,
                published_at=parse_publication_datetime(date_str),
                published_raw=date_str,
            )
        )
    return jobs


async def scrape_itjobs(queries: list[str], *, max_pages: int = 2) -> ScraperBatchResult:
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_ids: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    async with AsyncSession(impersonate="chrome120") as session:
        session.headers.update(DEFAULT_HEADERS)
        for q in queries:
            for page in range(1, max_pages + 1):
                params = {"q": q}
                if page > 1:
                    params["page"] = str(page)
                try:
                    res = await retry_async(session.get, ITJOBS_SEARCH, params=params, timeout=12)
                    if res.status_code == 404 and page > 1:
                        break
                    if res.status_code != 200:
                        failed += 1
                        warnings.append(f"ITJobs '{q}' p{page}: HTTP {res.status_code}")
                        break
                    ok += 1
                    parsed = parse_itjobs_html(res.text)
                    for j in parsed:
                        if j.job_id not in seen_ids:
                            seen_ids.add(j.job_id)
                            jobs.append(j)
                    if not parsed:
                        break
                except Exception as err:
                    failed += 1
                    warnings.append(f"ITJobs '{q}': {err}")
                    break
                await asyncio.sleep(0.35)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="ITJobs.pt",
            category="pt_board",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_landing_jobs_items(items: list[dict]) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        job_url = str(item.get("url") or "").strip()
        if not title or not job_url:
            continue

        # Extrair nome da empresa a partir do URL oficial: https://landing.jobs/at/<company-slug>/<job-slug>
        company = "Landing.jobs Partner"
        try:
            parts = [p for p in urlparse(job_url).path.split("/") if p]
            if len(parts) >= 2 and parts[0] == "at":
                company = parts[1].replace("-", " ").title()
        except ValueError:
            pass

        raw_locs = item.get("locations") or []
        all_locs: list[str] = []
        for loc in raw_locs:
            if isinstance(loc, dict):
                city = str(loc.get("city") or "").strip()
                cc = str(loc.get("country_code") or "").strip()
                formatted = ", ".join(p for p in (city, "Portugal" if cc.upper() == "PT" else cc) if p)
                if formatted:
                    all_locs.append(formatted)
        is_remote = bool(item.get("remote"))
        if is_remote and not all_locs:
            all_locs.append("Portugal (Remote)")
        primary_loc = next((l for l in all_locs if "portugal" in l.casefold() or "lisb" in l.casefold() or "set" in l.casefold()), all_locs[0] if all_locs else "Portugal")

        low_sal = item.get("gross_salary_low")
        high_sal = item.get("gross_salary_high")
        cc_sal = item.get("currency_code") or "EUR"
        salary = None
        if low_sal and high_sal:
            salary = f"{low_sal}-{high_sal} {cc_sal}"
        elif low_sal or high_sal:
            salary = f"{low_sal or high_sal} {cc_sal}"

        pub_raw = item.get("published_at") or item.get("updated_at")
        desc = clean_html_text(f"{item.get('main_requirements') or ''} {item.get('role_description') or ''}")
        tags = [str(t) for t in (item.get("tags") or []) if t]

        jobs.append(
            RawJob(
                source="Landing.jobs",
                source_type="pt_board",
                job_id=str(item.get("id") or job_url),
                title=title,
                company=company,
                location=primary_loc,
                all_locations=all_locs,
                job_url=job_url,
                modality="Remote" if is_remote else "On-site / Hybrid",
                is_remote=is_remote,
                employment_type=str(item.get("type") or "") or None,
                salary=salary,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=str(pub_raw or ""),
                tags=tags[:10],
                description=desc,
            )
        )
    return jobs


async def scrape_landing_jobs(*, max_pages: int = 6, page_size: int = 50) -> ScraperBatchResult:
    """Consome a API JSON oficial v1 da Landing.jobs."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for page in range(max_pages):
            offset = page * page_size
            try:
                res = await retry_async(
                    client.get,
                    LANDING_API_V1,
                    params={"limit": str(page_size), "offset": str(offset)},
                    timeout=12.0,
                )
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Landing.jobs offset={offset}: HTTP {res.status_code}")
                    break
                items = res.json()
                ok += 1
                if not isinstance(items, list) or not items:
                    break
                jobs.extend(parse_landing_jobs_items(items))
                if len(items) < page_size:
                    break
            except Exception as err:
                failed += 1
                warnings.append(f"Landing.jobs offset={offset}: {err}")
                break

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Landing.jobs API",
            category="pt_board",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def decode_net_empregos_bytes(content: bytes, content_type: str = "") -> str:
    """Descodifica HTML do Net-Empregos em cp1252/utf-8 sem corromper acentos portugueses."""
    ct_low = content_type.casefold()
    if "utf-8" in ct_low:
        return content.decode("utf-8", errors="replace")
    if "iso-8859" in ct_low or "windows-1252" in ct_low or "cp1252" in ct_low:
        return content.decode("cp1252", errors="replace")
    head = content[:2048].decode("ascii", errors="ignore").casefold()
    if "charset=utf-8" in head:
        return content.decode("utf-8", errors="replace")
    return content.decode("cp1252", errors="replace")


def parse_net_empregos_html(html: str, default_zone: str = "Portugal") -> list[RawJob]:
    soup = bs4.BeautifulSoup(html or "", "lxml")
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for card in soup.select(".job-item"):
        title_link = card.select_one("h2 a.oferta-link") or card.select_one("h2 a[href]")
        if not title_link:
            continue
        href = str(title_link.get("href") or "").strip()
        title = title_link.get_text(" ", strip=True)
        if not href or not title:
            continue
        job_url = href if href.startswith("http") else f"https://www.net-empregos.com{href}"
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        date_str = None
        loc_str = default_zone
        category_str = ""
        company_str = "Empresa Confidencial"

        for li in card.select(".job-ad-item li"):
            txt = li.get_text(" ", strip=True)
            if li.select_one(".flaticon-calendar"):
                date_str = txt
            elif li.select_one(".flaticon-pin"):
                loc_str = txt.strip("() ").strip() or default_zone
            elif li.select_one(".fa-tags"):
                category_str = txt
            elif li.select_one(".flaticon-work"):
                company_str = txt or company_str

        if company_str == "Empresa Confidencial":
            img = card.select_one("img[alt]")
            if img and img.get("alt"):
                company_str = str(img["alt"]).strip()

        # Se Net-Empregos mostrar "Todas as Zonas" numa pesquisa filtrada por zona, preserva a zona pesquisada
        if "todas as zonas" in loc_str.casefold() and default_zone != "Portugal":
            loc_str = f"{default_zone}, Portugal"
        elif "portugal" not in loc_str.casefold():
            loc_str = f"{loc_str}, Portugal"

        id_match = re.search(r"/(\d{5,})/", job_url)
        job_id = id_match.group(1) if id_match else job_url
        tags = [category_str] if category_str else []

        jobs.append(
            RawJob(
                source="Net-Empregos",
                source_type="pt_board",
                job_id=job_id,
                title=title,
                company=company_str,
                location=loc_str,
                all_locations=[loc_str],
                job_url=job_url,
                modality="On-site / Hybrid",
                is_remote="remoto" in title.casefold() or "teletrabalho" in title.casefold(),
                published_at=parse_publication_datetime(date_str),
                published_raw=date_str,
                tags=tags,
            )
        )
    return jobs


async def scrape_net_empregos(searches: list[dict]) -> ScraperBatchResult:
    """Recolhe vagas de Informática e Estágios no Net-Empregos (Setúbal e Lisboa)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for s in searches:
            params = {
                "chaves": s["keywords"],
                "cidade": s.get("zone_name", ""),
                "categoria": "0",
                "zona": str(s.get("zona", 0)),
                "tipo": "0",
            }
            try:
                res = await retry_async(client.get, NET_EMPREGOS_SEARCH, params=params, timeout=12.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Net-Empregos '{s['keywords']}': HTTP {res.status_code}")
                    continue
                ok += 1
                html = decode_net_empregos_bytes(res.content, res.headers.get("content-type", ""))
                for j in parse_net_empregos_html(html, default_zone=s.get("zone_name", "Portugal")):
                    if j.job_url not in seen_urls:
                        seen_urls.add(j.job_url)
                        jobs.append(j)
                await asyncio.sleep(0.35)
            except Exception as err:
                failed += 1
                warnings.append(f"Net-Empregos '{s['keywords']}': {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Net-Empregos",
            category="pt_board",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )
