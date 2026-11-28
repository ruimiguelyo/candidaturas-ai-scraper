"""Scrapers diretos de quadros ATS (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee, Teamtailor, Workday, Siemens Avature, SuccessFactors, Oracle HCM)."""

from __future__ import annotations

import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin

import bs4
import httpx

from estagio_radar.date_utils import parse_publication_datetime, utc_now
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import (
    DEFAULT_HEADERS,
    DomainCircuitBreaker,
    ETagCache,
    ScraperBatchResult,
    clean_html_text,
    retry_async,
)

_DETAIL_SKIP_TITLE_RE = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|staff|manager|director|head|architect|vp|"
    r"sales|account|legal|counsel|human\s+resources|hr|marketing|finance|payroll|"
    r"recruit(?:er|ing|ment)|physiotherapist|translator|nurse|controller)\b",
    re.IGNORECASE,
)
_EXPLICIT_ENTRY_TITLE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:intern|interns|internship|internships|estagio|estagios|estagiari[oa]|"
    r"trainee|graduate|junior|jr\.?|apprentice)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EXPLICIT_DOMAIN_TITLE_RE = re.compile(
    r"\b(?:software|ai|ia|ml|data|engineer|developer|cloud|cyber|security|python|java|tech|it)\b",
    re.IGNORECASE,
)


def _should_fetch_pt_detail(title: str, loc_blob: str, *, allow_generic_intern: bool = False) -> bool:
    """Decide se vale a pena descarregar a descrição de uma vaga PT não-sénior para evitar falsos negativos."""
    low_loc = loc_blob.casefold()
    if not any(k in low_loc for k in ("portugal", "lisbon", "lisboa", "setubal", "setúbal", "oeiras", "amadora", "moscavide", "carnaxide", "pt")):
        return False
    low_t = title.casefold()
    if "internal" in low_t or "international" in low_t:
        return False
    if _DETAIL_SKIP_TITLE_RE.search(title):
        return False
    if allow_generic_intern:
        return bool(_EXPLICIT_ENTRY_TITLE_RE.search(title)) and not bool(_EXPLICIT_DOMAIN_TITLE_RE.search(title))
    return not bool(_EXPLICIT_ENTRY_TITLE_RE.search(title))


def _infer_modality(text: str, is_remote: bool = False) -> str:
    low = text.casefold()
    if is_remote or "remote" in low or "remoto" in low:
        return "Remote"
    if "hybrid" in low or "híbrido" in low or "hibrido" in low:
        return "Hybrid"
    if "in-office" in low or "on-site" in low or "presencial" in low or "office" in low:
        return "On-site"
    return "Unknown"


async def scrape_greenhouse(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta boards públicos da Greenhouse com suporte ETag 304."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(10)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        cache_key = f"{url}?content=false"
        hdrs = etag_cache.request_headers(cache_key) if etag_cache else {}

        async with sem:
            try:
                res = await retry_async(client.get, url, params={"content": "false"}, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(cache_key)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Greenhouse {slug}: HTTP {res.status_code}")
                    return []
                payload = res.json()
                ok += 1
            except Exception as err:
                failed += 1
                warnings.append(f"Greenhouse {slug}: {err}")
                return []

        board_jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("absolute_url") or "").strip()
            if not title or not job_url:
                continue

            loc_name = str((item.get("location") or {}).get("name") or "").strip()
            all_locs: list[str] = [loc_name] if loc_name else []
            tags: list[str] = []

            for meta in item.get("metadata") or []:
                if not isinstance(meta, dict):
                    continue
                mname = str(meta.get("name") or "").casefold()
                mval = meta.get("value")
                vals = mval if isinstance(mval, list) else ([mval] if mval else [])
                for v in vals:
                    vs = str(v).strip()
                    if not vs:
                        continue
                    if "location" in mname or "office" in mname or "country" in mname:
                        if vs not in all_locs:
                            all_locs.append(vs)
                    elif "department" in mname or "team" in mname:
                        tags.append(vs)

            combined_loc = "; ".join(all_locs) if all_locs else "Unknown"
            display_loc = next(
                (l for l in all_locs if l.casefold() not in {"hybrid", "in-office", "on-site", "remote", ""}),
                loc_name or "Unknown",
            )
            desc_text = ""
            low_t = title.casefold()
            needs_office_detail = (
                display_loc.casefold() in {"hybrid", "in-office", "on-site", "unknown", ""}
                and any(w in low_t for w in ("intern", "estág", "estag", "trainee", "graduate", "apprentice", "junior"))
                and "internal" not in low_t
                and "international" not in low_t
            )
            if needs_office_detail or (len(board_jobs) < 1200 and _should_fetch_pt_detail(title, combined_loc)):
                try:
                    dres = await client.get(f"{url}/{item['id']}", timeout=8.0)
                    if dres.status_code == 200:
                        ddata = dres.json()
                        for off in ddata.get("offices") or []:
                            off_str = str(off.get("location") or off.get("name") or "").strip()
                            if off_str and off_str not in all_locs:
                                all_locs.append(off_str)
                        desc_text = clean_html_text(ddata.get("content"))
                        combined_loc = "; ".join(all_locs)
                        display_loc = next(
                            (l for l in all_locs if l.casefold() not in {"hybrid", "in-office", "on-site", "remote", ""}),
                            display_loc,
                        )
                except Exception:
                    pass

            pub_raw = item.get("updated_at") or item.get("first_published")
            pub_dt = parse_publication_datetime(pub_raw)
            is_rem = "remote" in combined_loc.casefold() or "home based" in combined_loc.casefold()

            board_jobs.append(
                RawJob(
                    source=f"Greenhouse ({company})",
                    source_type="ats",
                    job_id=str(item.get("id") or job_url),
                    title=title,
                    company=company,
                    location=display_loc,
                    all_locations=all_locs,
                    job_url=job_url,
                    modality=_infer_modality(f"{loc_name} {combined_loc}", is_rem),
                    is_remote=is_rem,
                    published_at=pub_dt,
                    published_raw=str(pub_raw or ""),
                    tags=tags[:8],
                    description=desc_text or None,
                )
            )

        if etag_cache:
            etag_cache.store(cache_key, res.headers, board_jobs)
        return board_jobs

    results = await asyncio.gather(*(fetch_board(b) for b in boards))
    for chunk in results:
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Greenhouse ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_ashby(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta boards públicos da AshbyHQ com suporte ETag 304."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(10)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}

        async with sem:
            try:
                res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(url)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Ashby {slug}: HTTP {res.status_code}")
                    return []
                payload = res.json()
                ok += 1
            except Exception as err:
                failed += 1
                warnings.append(f"Ashby {slug}: {err}")
                return []

        board_jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
            if not title or not job_url:
                continue

            loc = str(item.get("location") or "Unknown").strip()
            all_locs = [loc] if loc else []
            for sec in item.get("secondaryLocations") or []:
                if isinstance(sec, dict) and sec.get("location"):
                    all_locs.append(str(sec["location"]).strip())
                elif isinstance(sec, str) and sec.strip():
                    all_locs.append(sec.strip())

            is_rem = bool(item.get("isRemote")) or "remote" in str(item.get("workplaceType") or "").casefold()
            pub_raw = item.get("publishedAt")
            emp_type = str(item.get("employmentType") or "").strip() or None
            tags = [str(x) for x in (item.get("department"), item.get("team")) if x]
            desc = str(item.get("descriptionPlain") or "")[:1500]

            board_jobs.append(
                RawJob(
                    source=f"Ashby ({company})",
                    source_type="ats",
                    job_id=str(item.get("id") or job_url),
                    title=title,
                    company=company,
                    location=loc,
                    all_locations=all_locs,
                    job_url=job_url,
                    modality=_infer_modality(f"{loc} {item.get('workplaceType', '')}", is_rem),
                    is_remote=is_rem,
                    employment_type=emp_type,
                    published_at=parse_publication_datetime(pub_raw),
                    published_raw=str(pub_raw or ""),
                    tags=tags,
                    description=desc,
                )
            )
        if etag_cache:
            etag_cache.store(url, res.headers, board_jobs)
        return board_jobs

    results = await asyncio.gather(*(fetch_board(b) for b in boards))
    for chunk in results:
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Ashby ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_lever(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta boards públicos da Lever em paralelo com suporte ETag 304."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}
        async with sem:
            try:
                res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(url)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Lever {slug}: HTTP {res.status_code}")
                    return []
                items = res.json()
                ok += 1
            except Exception as err:
                failed += 1
                warnings.append(f"Lever {slug}: {err}")
                return []

        if not isinstance(items, list):
            return []
        board_jobs: list[RawJob] = []
        for item in items:
            title = str(item.get("text") or "").strip()
            job_url = str(item.get("hostedUrl") or item.get("applyUrl") or "").strip()
            if not title or not job_url:
                continue
            cats = item.get("categories") or {}
            loc = str(cats.get("location") or "Unknown").strip()
            all_locs = [str(x).strip() for x in (cats.get("allLocations") or []) if x]
            if loc and loc not in all_locs:
                all_locs.insert(0, loc)
            workplace = str(item.get("workplaceType") or "")
            is_rem = "remote" in workplace.casefold() or "remote" in loc.casefold()
            pub_raw = item.get("updatedAt") or item.get("createdAt")
            tags = [str(cats.get(k)) for k in ("team", "department") if cats.get(k)]

            board_jobs.append(
                RawJob(
                    source=f"Lever ({company})",
                    source_type="ats",
                    job_id=str(item.get("id") or job_url),
                    title=title,
                    company=company,
                    location=loc,
                    all_locations=all_locs,
                    job_url=job_url,
                    modality=_infer_modality(f"{workplace} {loc}", is_rem),
                    is_remote=is_rem,
                    employment_type=str(cats.get("commitment") or "") or None,
                    published_at=parse_publication_datetime(pub_raw),
                    published_raw=str(pub_raw or ""),
                    tags=tags,
                    description=str(item.get("descriptionPlain") or "")[:1500],
                )
            )
        if etag_cache:
            etag_cache.store(url, res.headers, board_jobs)
        return board_jobs

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Lever ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_smartrecruiters(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta SmartRecruiters PT em paralelo com ETag e descrição seletiva."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        board_jobs: list[RawJob] = []
        detail_fetches = 0
        offset = 0

        while offset < 300:
            cache_key = f"{url}?country=pt&offset={offset}"
            hdrs = etag_cache.request_headers(cache_key) if etag_cache else {}
            async with sem:
                try:
                    res = await retry_async(
                        client.get,
                        url,
                        params={"country": "pt", "limit": "100", "offset": str(offset)},
                        headers=hdrs,
                        timeout=10.0,
                    )
                    if res.status_code == 304 and etag_cache:
                        ok += 1
                        board_jobs.extend(etag_cache.get_cached_jobs(cache_key))
                        break
                    if res.status_code != 200:
                        failed += 1
                        warnings.append(f"SmartRecruiters {slug}: HTTP {res.status_code}")
                        break
                    data = res.json()
                    ok += 1
                except Exception as err:
                    failed += 1
                    warnings.append(f"SmartRecruiters {slug}: {err}")
                    break

            page_jobs: list[RawJob] = []
            content = data.get("content") or []
            for item in content:
                title = str(item.get("name") or "").strip()
                jid = str(item.get("id") or "").strip()
                if not title or not jid:
                    continue
                loc_obj = item.get("location") or {}
                city = str(loc_obj.get("city") or "").strip()
                region = str(loc_obj.get("region") or "").strip()
                full_loc = str(loc_obj.get("fullLocation") or ", ".join(p for p in (city, region, "Portugal") if p)).strip()
                is_rem = bool(loc_obj.get("remote"))
                is_hyb = bool(loc_obj.get("hybrid"))
                modality = "Remote" if is_rem else ("Hybrid" if is_hyb else "On-site")
                exp = (item.get("experienceLevel") or {}).get("label")
                emp = (item.get("typeOfEmployment") or {}).get("label")
                pub_raw = item.get("releasedDate")
                job_url = f"https://jobs.smartrecruiters.com/{slug}/{jid}"
                dept = (item.get("department") or {}).get("label")
                func = (item.get("function") or {}).get("label")
                tags = [str(x) for x in (dept, func) if x]

                desc_text = ""
                if detail_fetches < 6 and _should_fetch_pt_detail(title, full_loc or "Portugal"):
                    detail_fetches += 1
                    try:
                        dres = await client.get(f"{url}/{jid}", timeout=8.0)
                        if dres.status_code == 200:
                            sections = ((dres.json().get("jobAd") or {}).get("sections") or {})
                            raw_parts = [
                                str((sec or {}).get("text") or "")
                                for sec in sections.values()
                                if isinstance(sec, dict)
                            ]
                            desc_text = clean_html_text(" ".join(raw_parts), max_len=1500)
                    except Exception:
                        pass

                page_jobs.append(
                    RawJob(
                        source=f"SmartRecruiters ({company})",
                        source_type="ats",
                        job_id=jid,
                        title=title,
                        company=company,
                        location=full_loc or "Portugal",
                        all_locations=[full_loc] if full_loc else ["Portugal"],
                        job_url=job_url,
                        modality=modality,
                        is_remote=is_rem,
                        employment_type=str(emp) if emp else None,
                        seniority=str(exp) if exp else None,
                        published_at=parse_publication_datetime(pub_raw),
                        published_raw=str(pub_raw or ""),
                        tags=tags,
                        description=desc_text or None,
                    )
                )

            if etag_cache and offset == 0:
                etag_cache.store(cache_key, res.headers, page_jobs)
            board_jobs.extend(page_jobs)
            if len(content) < 100 or offset + 100 >= int(data.get("totalFound", 0)):
                break
            offset += 100
        return board_jobs

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="SmartRecruiters PT",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_recruitee(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta Recruitee JSON API em paralelo (ex: Mercedes-Benz.io)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://{slug}.recruitee.com/api/offers/"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}
        async with sem:
            try:
                res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(url)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Recruitee {slug}: HTTP {res.status_code}")
                    return []
                offers = res.json().get("offers") or []
                ok += 1
            except Exception as err:
                failed += 1
                warnings.append(f"Recruitee {slug}: {err}")
                return []

        board_jobs: list[RawJob] = []
        for item in offers:
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("careers_url") or item.get("careers_apply_url") or "").strip()
            if not title or not job_url:
                continue
            loc = str(item.get("location") or ", ".join(p for p in (item.get("city"), item.get("country")) if p) or "Unknown")
            is_rem = bool(item.get("remote"))
            pub_raw = item.get("updated_at") or item.get("published_at") or item.get("created_at")
            tags = [str(t) for t in (item.get("tags") or []) if t]
            if item.get("department"):
                tags.append(str(item["department"]))
            board_jobs.append(
                RawJob(
                    source=f"Recruitee ({company})",
                    source_type="ats",
                    job_id=str(item.get("id") or job_url),
                    title=title,
                    company=company,
                    location=loc,
                    all_locations=[loc],
                    job_url=job_url,
                    modality="Remote" if is_rem else ("Hybrid" if item.get("hybrid") else "On-site / Hybrid"),
                    is_remote=is_rem,
                    employment_type=str(item.get("employment_type_code") or "") or None,
                    published_at=parse_publication_datetime(pub_raw),
                    published_raw=str(pub_raw or ""),
                    tags=tags[:8],
                    description=clean_html_text(item.get("description")),
                )
            )
        if etag_cache:
            etag_cache.store(url, res.headers, board_jobs)
        return board_jobs

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Recruitee ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_teamtailor_rss(xml_text: str, company: str) -> list[RawJob]:
    """Faz parsing ao XML RSS da Teamtailor, incluindo <tt:locations><tt:location> aninhado."""
    root = ET.fromstring(xml_text)
    jobs: list[RawJob] = []

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub_raw = item.findtext("pubDate") or ""
        desc = clean_html_text(item.findtext("description"))
        all_locs: list[str] = []
        remote_flag = False
        tags: list[str] = []

        # Extrair <tt:location> aninhado dentro de <tt:locations>, além de tags planas
        for node in item.iter():
            tag_low = node.tag.split("}")[-1].casefold()
            if tag_low == "location":
                city = ""
                country = ""
                name = ""
                for sub in node:
                    sub_tag = sub.tag.split("}")[-1].casefold()
                    sub_txt = (sub.text or "").strip()
                    if sub_tag == "city":
                        city = sub_txt
                    elif sub_tag == "country":
                        country = sub_txt
                    elif sub_tag == "name":
                        name = sub_txt
                formatted = ", ".join(p for p in (city, country) if p) or name or (node.text or "").strip()
                if formatted and formatted not in all_locs:
                    all_locs.append(formatted)
            elif tag_low == "remotestatus" and node.text:
                if "remote" in node.text.casefold() or "fully" in node.text.casefold():
                    remote_flag = True
            elif tag_low in {"department", "role"} and node.text:
                t = node.text.strip()
                if t and t not in tags:
                    tags.append(t)

        loc = all_locs[0] if all_locs else "Unknown"
        if loc == "Unknown" and "remote" in desc.casefold():
            loc = "Remote"
            all_locs = ["Remote"]
            remote_flag = True

        jobs.append(
            RawJob(
                source=f"Teamtailor ({company})",
                source_type="ats",
                job_id=link.rstrip("/").split("/")[-1],
                title=title,
                company=company,
                location=loc,
                all_locations=all_locs or [loc],
                job_url=link,
                modality="Remote" if remote_flag else "On-site / Hybrid",
                is_remote=remote_flag,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
                tags=tags[:8],
                description=desc,
            )
        )
    return jobs


async def scrape_teamtailor(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta feeds RSS do Teamtailor em paralelo (TEKEVER, Coverflex, Infraspeak, Stratio)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        domain = board["domain"]
        company = board.get("company") or domain
        url = f"https://{domain}/jobs.rss"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}
        async with sem:
            try:
                res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(url)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Teamtailor {domain}: HTTP {res.status_code}")
                    return []
                parsed = parse_teamtailor_rss(res.text, company)
                ok += 1
                if etag_cache:
                    etag_cache.store(url, res.headers, parsed)
                return parsed
            except Exception as err:
                failed += 1
                warnings.append(f"Teamtailor {domain}: {err}")
                return []

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Teamtailor RSS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_workday(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    circuit_breaker: DomainCircuitBreaker | None = None,
) -> ScraperBatchResult:
    """Consulta endpoints Workday CXS JSON com paginação real além de 20 e proteção anti-429."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    try:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession
    except ImportError:
        CurlAsyncSession = None

    async def _fetch_search(session: Any, tenant: str, wd: str, site: str, company: str, search_text: str, max_items: int) -> None:
        nonlocal ok, failed
        domain_key = f"{tenant}.{wd}.myworkdayjobs.com"
        if circuit_breaker and circuit_breaker.is_open(domain_key):
            warnings.append(f"Workday {company}: cooldown anti-429 ativo")
            return
        api_url = f"https://{domain_key}/wday/cxs/{tenant}/{site}/jobs"
        offset = 0
        total = None

        while offset < max_items:
            payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search_text}
            if offset > 0:
                await asyncio.sleep(0.25)
            try:
                res = await session.post(
                    api_url,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=12,
                )
            except Exception as err:
                if session is not client:
                    try:
                        res = await retry_async(
                            client.post,
                            api_url,
                            json=payload,
                            headers={"Accept": "application/json", "Content-Type": "application/json"},
                            timeout=12.0,
                        )
                    except Exception as fb_err:
                        failed += 1
                        warnings.append(f"Workday {company}: {fb_err}")
                        break
                else:
                    failed += 1
                    warnings.append(f"Workday {company}: {err}")
                    break

            try:
                if res.status_code == 429:
                    failed += 1
                    if circuit_breaker:
                        circuit_breaker.record_rate_limit(domain_key, base_cooldown=180.0)
                    warnings.append(f"Workday {company}: HTTP 429 (rate-limit WAF, cooldown aplicado)")
                    break
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Workday {company}: HTTP {res.status_code}")
                    break
                data = res.json()
                ok += 1
                if circuit_breaker:
                    circuit_breaker.record_success(domain_key)
            except Exception as err:
                failed += 1
                warnings.append(f"Workday {company}: {err}")
                break

            if total is None:
                total = int(data.get("total") or 0)
            postings = data.get("jobPostings") or []
            for post in postings:
                title = str(post.get("title") or "").strip()
                ext_path = str(post.get("externalPath") or "").strip()
                if not title or not ext_path:
                    continue
                job_url = f"https://{domain_key}/en-US/{site}{ext_path}"
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                loc = str(post.get("locationsText") or "Portugal").strip()
                pub_raw = str(post.get("postedOn") or "").replace("Posted ", "").strip()
                jobs.append(
                    RawJob(
                        source=f"Workday ({company})",
                        source_type="ats",
                        job_id=ext_path.rstrip("/").split("/")[-1],
                        title=title,
                        company=company,
                        location=loc,
                        all_locations=[loc],
                        job_url=job_url,
                        modality=_infer_modality(loc),
                        is_remote="remote" in loc.casefold(),
                        published_at=parse_publication_datetime(pub_raw),
                        published_raw=pub_raw,
                    )
                )

            offset += 20
            if len(postings) < 20 or (total is not None and offset >= total):
                break

        if search_text == "Portugal" and total and total > max_items:
            await _fetch_search(session, tenant, wd, site, company, "intern", 40)

    async def _run_queries(session: Any) -> None:
        for idx, board in enumerate(boards):
            if idx > 0:
                await asyncio.sleep(0.95)
            tenant = board["tenant"]
            wd = board["wd"]
            site = board["site"]
            company = board.get("company") or tenant
            search_text = board.get("search") or "Portugal"
            await _fetch_search(session, tenant, wd, site, company, search_text, 60)

    if CurlAsyncSession is not None:
        async with CurlAsyncSession(impersonate="chrome") as curl_session:
            await _run_queries(curl_session)
    else:
        await _run_queries(client)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Workday CXS PT",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_siemens_avature_html(html: str, *, now: datetime | None = None) -> list[RawJob]:
    """Extrai vagas do portal Avature oficial da Siemens."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    ref = now or utc_now()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for art in soup.select("article.article--result"):
        link = art.select_one("h3 a[href*='JobDetail']") or art.select_one("a[href*='JobDetail']")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        href = str(link.get("href") or "").strip()
        if not title or not href:
            continue
        job_url = urljoin("https://jobs.siemens.com", href)
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        sub = art.select_one(".article__header__text__subtitle")
        sub_txt = sub.get_text(" ", strip=True) if sub else ""
        # Exemplo: "Amadora , Lisboa , Portugal • Job ID: 527474 • Internal Services"
        parts = [p.strip() for p in sub_txt.split("•") if p.strip()]
        loc = re.sub(r"\s*,\s*", ", ", parts[0]) if parts else "Portugal"
        jid = job_url.rstrip("/").split("/")[-1]
        tags: list[str] = []
        for p in parts[1:]:
            jm = re.search(r"Job ID:\s*(\d+)", p, re.I)
            if jm:
                jid = jm.group(1)
            else:
                tags.append(p)

        jobs.append(
            RawJob(
                source="Siemens Avature",
                source_type="ats",
                job_id=jid,
                title=title,
                company="Siemens",
                location=loc,
                all_locations=[loc],
                job_url=job_url,
                modality=_infer_modality(loc),
                is_remote="remote" in loc.casefold(),
                published_at=ref - timedelta(days=7),
                published_raw="Live ATS (Siemens)",
                tags=tags,
            )
        )
    return jobs


async def scrape_siemens_avature(cfg: dict[str, Any], client: httpx.AsyncClient) -> ScraperBatchResult:
    t0 = time.monotonic()
    base_url = str(cfg.get("base_url") or "https://jobs.siemens.com/en_US/externaljobs/SearchJobs")
    queries = list(cfg.get("queries") or ["Portugal intern"])
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    for q in queries:
        url = f"{base_url.rstrip('/')}/{quote(q)}"
        try:
            res = await retry_async(client.get, url, params={"listFilterMode": "1"}, timeout=12.0)
            if res.status_code != 200:
                failed += 1
                warnings.append(f"Siemens '{q}': HTTP {res.status_code}")
                continue
            ok += 1
            for j in parse_siemens_avature_html(res.text):
                if j.job_url not in seen_urls:
                    seen_urls.add(j.job_url)
                    jobs.append(j)
        except Exception as err:
            failed += 1
            warnings.append(f"Siemens '{q}': {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Siemens Avature PT",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_successfactors_html(html: str, base_url: str, company: str) -> list[RawJob]:
    """Extrai vagas de portais SAP SuccessFactors RMK (EDP, Miniclip, Galp)."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for row in soup.select("tr.data-row"):
        link = row.select_one("a.jobTitle-link")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        href = str(link.get("href") or "").strip()
        if not title or not href:
            continue
        job_url = urljoin(base_url, href)
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        loc_el = row.select_one("span.jobLocation")
        loc = re.sub(r"\s+", " ", loc_el.get_text(" ", strip=True)) if loc_el else "Portugal"
        date_el = row.select_one("span.jobDate")
        pub_raw = date_el.get_text(" ", strip=True) if date_el else ""

        jobs.append(
            RawJob(
                source=f"SuccessFactors ({company})",
                source_type="ats",
                job_id=job_url.rstrip("/").split("/")[-1],
                title=title,
                company=company,
                location=loc,
                all_locations=[loc],
                job_url=job_url,
                modality=_infer_modality(loc),
                is_remote="remote" in loc.casefold(),
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
            )
        )
    return jobs


async def scrape_successfactors(boards: list[dict[str, Any]], client: httpx.AsyncClient) -> ScraperBatchResult:
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(5)

    async def fetch_one(board: dict[str, Any], q: str) -> list[RawJob]:
        nonlocal ok, failed
        base_url = board["base_url"]
        company = board["company"]
        async with sem:
            try:
                res = await retry_async(client.get, base_url, params={"q": q}, timeout=12.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"SuccessFactors {company} '{q}': HTTP {res.status_code}")
                    return []
                ok += 1
                parsed = parse_successfactors_html(res.text, base_url, company)
            except Exception as err:
                failed += 1
                warnings.append(f"SuccessFactors {company}: {err}")
                return []

        detail_cnt = 0
        for j in parsed:
            if detail_cnt < 5 and _should_fetch_pt_detail(j.title, j.location, allow_generic_intern=True):
                detail_cnt += 1
                try:
                    dres = await client.get(j.job_url, timeout=8.0)
                    if dres.status_code == 200:
                        dsoup = bs4.BeautifulSoup(dres.text, "lxml")
                        desc_node = dsoup.select_one(".jobdescription") or dsoup.select_one("[itemprop='description']")
                        j.description = clean_html_text(str(desc_node or dsoup.body or ""), max_len=1500)
                except Exception:
                    pass
        return parsed

    coros = [fetch_one(board, q) for board in boards for q in (board.get("queries") or [""])]
    for chunk in await asyncio.gather(*coros):
        for j in chunk:
            if j.job_url not in seen_urls:
                seen_urls.add(j.job_url)
                jobs.append(j)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="SuccessFactors PT",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_oracle_hcm_json(payload: dict[str, Any], job_base_url: str, company: str) -> list[RawJob]:
    """Extrai vagas em Portugal de endpoints Oracle Recruiting Cloud (ex: Nokia)."""
    jobs: list[RawJob] = []
    items = payload.get("items") or []
    reqs = items[0].get("requisitionList") or [] if items and isinstance(items[0], dict) else []

    for req in reqs:
        title = str(req.get("Title") or "").strip()
        req_id = str(req.get("Id") or "").strip()
        if not title or not req_id:
            continue
        prim_loc = str(req.get("PrimaryLocation") or "").strip()
        sec_locs = [
            str(s.get("Name") or s.get("LocationName") or "").strip()
            for s in (req.get("secondaryLocations") or [])
            if isinstance(s, dict)
        ]
        all_locs = [l for l in [prim_loc, *sec_locs] if l]
        combined = " ".join(all_locs).casefold()
        if not any(k in combined for k in ("portugal", "lisbon", "lisboa", "amadora", "setubal", "remote")):
            continue
        pub_raw = str(req.get("PostedDate") or "").strip()
        job_url = f"{job_base_url.rstrip('/')}/{req_id}"
        jobs.append(
            RawJob(
                source=f"OracleHCM ({company})",
                source_type="ats",
                job_id=req_id,
                title=title,
                company=company,
                location=prim_loc or "Portugal",
                all_locations=all_locs or ["Portugal"],
                job_url=job_url,
                modality=_infer_modality(combined),
                is_remote="remote" in combined,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
            )
        )
    return jobs


async def scrape_oracle_hcm(boards: list[dict[str, Any]], client: httpx.AsyncClient) -> ScraperBatchResult:
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(4)

    async def fetch_board(board: dict[str, Any]) -> list[RawJob]:
        nonlocal ok, failed
        company = board["name"]
        endpoint = board["endpoint"]
        site = board.get("site_number", "CX_1")
        limit = int(board.get("limit", 200))
        finder = f"findReqs;siteNumber={site},limit={limit}"
        url = f"{endpoint}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
        async with sem:
            try:
                res = await retry_async(client.get, url, timeout=15.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"OracleHCM {company}: HTTP {res.status_code}")
                    return []
                ok += 1
                return parse_oracle_hcm_json(res.json(), board["job_base_url"], company)
            except Exception as err:
                failed += 1
                warnings.append(f"OracleHCM {company}: {err}")
                return []

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Oracle HCM PT",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_revolut_next_data(html: str, *, now: datetime | None = None) -> list[RawJob]:
    """Extrai posições oficiais diretamente do __NEXT_DATA__ do portal Revolut Careers."""
    ref = now or utc_now()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html or "", re.DOTALL)
    if not m:
        return []
    try:
        nd = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    positions = ((nd.get("props") or {}).get("pageProps") or {}).get("positions") or []
    jobs: list[RawJob] = []
    seen_ids: set[str] = set()
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        jid = str(pos.get("id") or "").strip()
        title = str(pos.get("text") or pos.get("title") or "").strip()
        if not jid or not title or jid in seen_ids:
            continue
        seen_ids.add(jid)

        all_locs: list[str] = []
        is_remote = False
        for loc in pos.get("locations") or []:
            if not isinstance(loc, dict):
                continue
            lname = str(loc.get("name") or loc.get("country") or "").strip()
            lcountry = str(loc.get("country") or "").strip()
            ltype = str(loc.get("type") or "").casefold()
            if ltype == "remote" or "remote" in lname.casefold():
                is_remote = True
            if lname:
                all_locs.append(lname)
            elif lcountry:
                all_locs.append(lcountry)

        pt_loc = next(
            (l for l in all_locs if any(k in l.casefold() for k in ("portugal", "lisbon", "lisboa", "porto"))),
            all_locs[0] if all_locs else "Remote",
        )
        team = str(pos.get("team") or "").strip()
        desc = clean_html_text(pos.get("description") or "")
        pub_raw = str(pos.get("updatedAt") or pos.get("createdAt") or pos.get("publishedAt") or "").strip()
        pub_dt = parse_publication_datetime(pub_raw) if pub_raw else (ref - timedelta(days=2))

        jobs.append(
            RawJob(
                source="Revolut",
                source_type="ats",
                job_id=jid,
                title=title,
                company="Revolut",
                location=pt_loc,
                all_locations=all_locs or [pt_loc],
                job_url=f"https://www.revolut.com/careers/apply/{jid}/",
                modality="Remote" if is_remote else "Hybrid",
                is_remote=is_remote,
                published_at=pub_dt,
                published_raw=pub_raw or "Live Revolut Careers",
                tags=[team] if team else [],
                description=desc or None,
            )
        )
    return jobs


async def scrape_revolut(
    cfg: dict[str, Any],
    *,
    etag_cache: ETagCache | None = None,
    circuit_breaker: DomainCircuitBreaker | None = None,
) -> ScraperBatchResult:
    """Consulta posições diretas em Revolut Careers com curl_cffi."""
    t0 = time.monotonic()
    if not cfg or not cfg.get("enabled", True):
        return ScraperBatchResult(jobs=[], health=SourceHealth(source="Revolut Direct", category="ats"))

    url = "https://www.revolut.com/careers/"
    if circuit_breaker and circuit_breaker.is_open("www.revolut.com"):
        return ScraperBatchResult(
            jobs=[],
            health=SourceHealth(source="Revolut Direct", category="ats", warnings=["Revolut em cooldown anti-429."]),
        )

    try:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession
    except ImportError:
        return ScraperBatchResult(
            jobs=[],
            health=SourceHealth(source="Revolut Direct", category="ats", warnings=["curl_cffi indisponível para Revolut."]),
        )

    hdrs = etag_cache.request_headers(url) if etag_cache else {}
    ok = failed = 0
    warnings: list[str] = []
    jobs: list[RawJob] = []

    try:
        async with CurlAsyncSession(impersonate="chrome") as cs:
            res = await cs.get(url, headers=hdrs, timeout=14)
            if res.status_code == 304 and etag_cache:
                ok = 1
                jobs = etag_cache.get_cached_jobs(url)
            elif res.status_code == 429:
                failed = 1
                if circuit_breaker:
                    circuit_breaker.record_rate_limit("www.revolut.com", base_cooldown=180.0)
                warnings.append("Revolut HTTP 429")
            elif res.status_code != 200:
                failed = 1
                warnings.append(f"Revolut HTTP {res.status_code}")
            else:
                ok = 1
                if circuit_breaker:
                    circuit_breaker.record_success("www.revolut.com")
                jobs = parse_revolut_next_data(res.text)
                if etag_cache and jobs:
                    etag_cache.store(url, res.headers, jobs)
    except Exception as err:
        failed = 1
        warnings.append(f"Revolut: {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Revolut Direct",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_workable_json(payload: dict[str, Any], slug: str, company: str) -> list[RawJob]:
    """Faz parsing ao JSON v3 público da Workable."""
    jobs: list[RawJob] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        shortcode = str(item.get("shortcode") or item.get("id") or "").strip()
        if not title or not shortcode:
            continue
        locs: list[str] = []
        loc_obj = item.get("location")
        if isinstance(loc_obj, dict):
            s = ", ".join(p for p in (loc_obj.get("city"), loc_obj.get("country")) if p)
            if s:
                locs.append(s)
        for extra in item.get("locations") or []:
            if isinstance(extra, dict):
                s = ", ".join(p for p in (extra.get("city"), extra.get("country")) if p)
                if s and s not in locs:
                    locs.append(s)
        is_rem = bool(item.get("remote"))
        primary_loc = locs[0] if locs else ("Remote" if is_rem else "Unknown")
        pub_raw = str(item.get("published") or "").strip()
        dept = item.get("department") or []
        tags = [str(d) for d in dept] if isinstance(dept, list) else ([str(dept)] if dept else [])

        jobs.append(
            RawJob(
                source=f"Workable ({company})",
                source_type="ats",
                job_id=shortcode,
                title=title,
                company=company,
                location=primary_loc,
                all_locations=locs or [primary_loc],
                job_url=f"https://apply.workable.com/{slug}/j/{shortcode}/",
                modality="Remote" if is_rem else "On-site / Hybrid",
                is_remote=is_rem,
                employment_type=str(item.get("type") or "") or None,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
                tags=tags[:6],
            )
        )
    return jobs


async def scrape_workable(boards: list[dict[str, str]], client: httpx.AsyncClient) -> ScraperBatchResult:
    """Consulta quadros públicos Workable API v3 em paralelo."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
        async with sem:
            try:
                res = await retry_async(
                    client.post,
                    url,
                    json={"query": "", "location": [], "department": [], "worktype": [], "remote": []},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=10.0,
                )
                if res.status_code != 200 or not res.content:
                    failed += 1
                    warnings.append(f"Workable {slug}: HTTP {res.status_code}")
                    return []
                ok += 1
                return parse_workable_json(res.json(), slug, company)
            except Exception as err:
                failed += 1
                warnings.append(f"Workable {slug}: {err}")
                return []

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Workable ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def parse_personio_xml(xml_text: str, slug: str, company: str) -> list[RawJob]:
    """Faz parsing ao feed XML público da Personio."""
    if not xml_text or "<position>" not in xml_text:
        return []
    root = ET.fromstring(xml_text)
    jobs: list[RawJob] = []
    for pos in root.findall(".//position"):
        jid = (pos.findtext("id") or "").strip()
        title = (pos.findtext("name") or "").strip()
        if not jid or not title:
            continue
        office = (pos.findtext("office") or "").strip()
        dept = (pos.findtext("department") or "").strip()
        emp_type = (pos.findtext("employmentType") or "").strip() or None
        pub_raw = (pos.findtext("createdAt") or "").strip()
        desc_parts = [clean_html_text(d.findtext("value")) for d in pos.findall(".//jobDescription")]
        desc = " ".join(p for p in desc_parts if p)[:1500]
        is_rem = "remote" in office.casefold()
        jobs.append(
            RawJob(
                source=f"Personio ({company})",
                source_type="ats",
                job_id=jid,
                title=title,
                company=company,
                location=office or "Unknown",
                all_locations=[office] if office else [],
                job_url=f"https://{slug}.jobs.personio.de/job/{jid}",
                modality="Remote" if is_rem else "On-site / Hybrid",
                is_remote=is_rem,
                employment_type=emp_type,
                published_at=parse_publication_datetime(pub_raw),
                published_raw=pub_raw,
                tags=[dept] if dept else [],
                description=desc or None,
            )
        )
    return jobs


async def scrape_personio(
    boards: list[dict[str, str]],
    client: httpx.AsyncClient,
    etag_cache: ETagCache | None = None,
) -> ScraperBatchResult:
    """Consulta feeds XML públicos da Personio em paralelo."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []
    sem = asyncio.Semaphore(6)

    async def fetch_board(board: dict[str, str]) -> list[RawJob]:
        nonlocal ok, failed
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://{slug}.jobs.personio.de/xml"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}
        async with sem:
            try:
                res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
                if res.status_code == 304 and etag_cache:
                    ok += 1
                    return etag_cache.get_cached_jobs(url)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Personio {slug}: HTTP {res.status_code}")
                    return []
                parsed = parse_personio_xml(res.text, slug, company)
                ok += 1
                if etag_cache:
                    etag_cache.store(url, res.headers, parsed)
                return parsed
            except Exception as err:
                failed += 1
                warnings.append(f"Personio {slug}: {err}")
                return []

    for chunk in await asyncio.gather(*(fetch_board(b) for b in boards)):
        jobs.extend(chunk)

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="Personio ATS",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


async def scrape_all_ats(
    sources_cfg: dict[str, Any],
    *,
    etag_cache: ETagCache | None = None,
    include_workday: bool = True,
    include_slow: bool = True,
    circuit_breaker: DomainCircuitBreaker | None = None,
) -> list[ScraperBatchResult]:
    """Corre quadros ATS diretos em paralelo (camada rápida para Turbo-Sniper e camada pesada para fast/full)."""
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        tasks: list[Any] = [
            scrape_greenhouse(sources_cfg.get("greenhouse") or [], client, etag_cache=etag_cache),
            scrape_ashby(sources_cfg.get("ashby") or [], client, etag_cache=etag_cache),
            scrape_lever(sources_cfg.get("lever") or [], client, etag_cache=etag_cache),
            scrape_smartrecruiters(sources_cfg.get("smartrecruiters") or [], client, etag_cache=etag_cache),
            scrape_recruitee(sources_cfg.get("recruitee") or [], client, etag_cache=etag_cache),
            scrape_teamtailor(sources_cfg.get("teamtailor") or [], client, etag_cache=etag_cache),
        ]
        if sources_cfg.get("workable"):
            tasks.append(scrape_workable(sources_cfg.get("workable") or [], client))
        if sources_cfg.get("personio"):
            tasks.append(scrape_personio(sources_cfg.get("personio") or [], client, etag_cache=etag_cache))
        if (sources_cfg.get("revolut") or {}).get("enabled"):
            tasks.append(scrape_revolut(sources_cfg.get("revolut") or {}, etag_cache=etag_cache, circuit_breaker=circuit_breaker))
        if include_slow:
            tasks.extend(
                [
                    scrape_siemens_avature(sources_cfg.get("siemens_avature") or {}, client),
                    scrape_successfactors(sources_cfg.get("successfactors") or [], client),
                    scrape_oracle_hcm(sources_cfg.get("oracle_hcm") or [], client),
                ]
            )
        if include_workday:
            tasks.append(scrape_workday(sources_cfg.get("workday") or [], client, circuit_breaker=circuit_breaker))
        return list(await asyncio.gather(*tasks))
