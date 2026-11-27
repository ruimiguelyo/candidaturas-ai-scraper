"""Scrapers diretos de quadros ATS (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee, Teamtailor, Workday, Siemens Avature, SuccessFactors, Oracle HCM)."""

from __future__ import annotations

import asyncio
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
    ETagCache,
    ScraperBatchResult,
    clean_html_text,
    retry_async,
)


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
            if (
                display_loc.casefold() in {"hybrid", "in-office", "on-site", "unknown", ""}
                and any(w in low_t for w in ("intern", "estág", "estag", "trainee", "graduate", "apprentice", "junior"))
                and "internal" not in low_t
                and "international" not in low_t
            ):
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

            pub_raw = item.get("first_published") or item.get("updated_at")
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


async def scrape_lever(boards: list[dict[str, str]], client: httpx.AsyncClient) -> ScraperBatchResult:
    """Consulta boards públicos da Lever."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    for board in boards:
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            res = await retry_async(client.get, url, timeout=10.0)
            if res.status_code != 200:
                failed += 1
                warnings.append(f"Lever {slug}: HTTP {res.status_code}")
                continue
            items = res.json()
            ok += 1
        except Exception as err:
            failed += 1
            warnings.append(f"Lever {slug}: {err}")
            continue

        if not isinstance(items, list):
            continue
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
            pub_raw = item.get("createdAt")
            tags = [str(cats.get(k)) for k in ("team", "department") if cats.get(k)]

            jobs.append(
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


async def scrape_smartrecruiters(boards: list[dict[str, str]], client: httpx.AsyncClient) -> ScraperBatchResult:
    """Consulta SmartRecruiters filtrado por Portugal (country=pt)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    for board in boards:
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        offset = 0
        while offset < 300:
            try:
                res = await retry_async(
                    client.get,
                    url,
                    params={"country": "pt", "limit": "100", "offset": str(offset)},
                    timeout=10.0,
                )
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

                jobs.append(
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
                    )
                )

            if len(content) < 100 or offset + 100 >= int(data.get("totalFound", 0)):
                break
            offset += 100

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


async def scrape_recruitee(boards: list[dict[str, str]], client: httpx.AsyncClient) -> ScraperBatchResult:
    """Consulta Recruitee JSON API (ex: Mercedes-Benz.io)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    for board in boards:
        slug = board["slug"]
        company = board.get("company") or slug
        url = f"https://{slug}.recruitee.com/api/offers/"
        try:
            res = await retry_async(client.get, url, timeout=10.0)
            if res.status_code != 200:
                failed += 1
                warnings.append(f"Recruitee {slug}: HTTP {res.status_code}")
                continue
            offers = res.json().get("offers") or []
            ok += 1
        except Exception as err:
            failed += 1
            warnings.append(f"Recruitee {slug}: {err}")
            continue

        for item in offers:
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("careers_url") or item.get("careers_apply_url") or "").strip()
            if not title or not job_url:
                continue
            loc = str(item.get("location") or ", ".join(p for p in (item.get("city"), item.get("country")) if p) or "Unknown")
            is_rem = bool(item.get("remote"))
            pub_raw = item.get("published_at") or item.get("created_at")
            tags = [str(t) for t in (item.get("tags") or []) if t]
            if item.get("department"):
                tags.append(str(item["department"]))
            jobs.append(
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
    """Consulta feeds RSS do Teamtailor (TEKEVER, Coverflex, Infraspeak, Stratio)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    for board in boards:
        domain = board["domain"]
        company = board.get("company") or domain
        url = f"https://{domain}/jobs.rss"
        hdrs = etag_cache.request_headers(url) if etag_cache else {}
        try:
            res = await retry_async(client.get, url, headers=hdrs, timeout=10.0)
            if res.status_code == 304 and etag_cache:
                ok += 1
                jobs.extend(etag_cache.get_cached_jobs(url))
                continue
            if res.status_code != 200:
                failed += 1
                warnings.append(f"Teamtailor {domain}: HTTP {res.status_code}")
                continue
            parsed = parse_teamtailor_rss(res.text, company)
            ok += 1
            if etag_cache:
                etag_cache.store(url, res.headers, parsed)
            jobs.extend(parsed)
        except Exception as err:
            failed += 1
            warnings.append(f"Teamtailor {domain}: {err}")

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


async def scrape_workday(boards: list[dict[str, str]], client: httpx.AsyncClient) -> ScraperBatchResult:
    """Consulta endpoints Workday CXS JSON filtrando por Portugal com curl_cffi (anti-WAF 429)."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    try:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession
    except ImportError:
        CurlAsyncSession = None

    async def _run_queries(session: Any) -> None:
        nonlocal ok, failed
        for idx, board in enumerate(boards):
            tenant = board["tenant"]
            wd = board["wd"]
            site = board["site"]
            company = board.get("company") or tenant
            api_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            search_text = board.get("search") or "Portugal"
            payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": search_text}
            if idx > 0:
                await asyncio.sleep(1.2)
            try:
                res = await session.post(
                    api_url,
                    json=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    timeout=12,
                )
                if res.status_code == 429:
                    failed += 1
                    warnings.append(f"Workday {company}: HTTP 429 (rate-limit WAF, paragem graciosa)")
                    break
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"Workday {company}: HTTP {res.status_code}")
                    continue
                data = res.json()
                ok += 1
            except Exception as err:
                failed += 1
                warnings.append(f"Workday {company}: {err}")
                continue

            for post in data.get("jobPostings") or []:
                title = str(post.get("title") or "").strip()
                ext_path = str(post.get("externalPath") or "").strip()
                if not title or not ext_path:
                    continue
                job_url = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{ext_path}"
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

    for board in boards:
        base_url = board["base_url"]
        company = board["company"]
        for q in board.get("queries") or [""]:
            try:
                res = await retry_async(client.get, base_url, params={"q": q}, timeout=12.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"SuccessFactors {company} '{q}': HTTP {res.status_code}")
                    continue
                ok += 1
                for j in parse_successfactors_html(res.text, base_url, company):
                    if j.job_url not in seen_urls:
                        seen_urls.add(j.job_url)
                        jobs.append(j)
            except Exception as err:
                failed += 1
                warnings.append(f"SuccessFactors {company}: {err}")

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

    for board in boards:
        company = board["name"]
        endpoint = board["endpoint"]
        site = board.get("site_number", "CX_1")
        limit = int(board.get("limit", 200))
        finder = f"findReqs;siteNumber={site},limit={limit}"
        url = f"{endpoint}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
        try:
            res = await retry_async(client.get, url, timeout=15.0)
            if res.status_code != 200:
                failed += 1
                warnings.append(f"OracleHCM {company}: HTTP {res.status_code}")
                continue
            ok += 1
            jobs.extend(parse_oracle_hcm_json(res.json(), board["job_base_url"], company))
        except Exception as err:
            failed += 1
            warnings.append(f"OracleHCM {company}: {err}")

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


async def scrape_all_ats(
    sources_cfg: dict[str, Any],
    *,
    etag_cache: ETagCache | None = None,
    include_workday: bool = True,
) -> list[ScraperBatchResult]:
    """Corre todos os quadros ATS diretos em paralelo com uma única sessão HTTP."""
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        tasks = [
            scrape_greenhouse(sources_cfg.get("greenhouse") or [], client, etag_cache=etag_cache),
            scrape_ashby(sources_cfg.get("ashby") or [], client, etag_cache=etag_cache),
            scrape_lever(sources_cfg.get("lever") or [], client),
            scrape_smartrecruiters(sources_cfg.get("smartrecruiters") or [], client),
            scrape_recruitee(sources_cfg.get("recruitee") or [], client),
            scrape_teamtailor(sources_cfg.get("teamtailor") or [], client, etag_cache=etag_cache),
            scrape_siemens_avature(sources_cfg.get("siemens_avature") or {}, client),
            scrape_successfactors(sources_cfg.get("successfactors") or [], client),
            scrape_oracle_hcm(sources_cfg.get("oracle_hcm") or [], client),
        ]
        if include_workday:
            tasks.append(scrape_workday(sources_cfg.get("workday") or [], client))
        return list(await asyncio.gather(*tasks))
