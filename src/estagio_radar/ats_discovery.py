"""Auto-descoberta validada de boards ATS a partir de URLs externas de candidaturas."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from estagio_radar.scrapers.base import DEFAULT_HEADERS

DISCOVERED_ATS_PATH = Path("data/discovered_ats.json")

ATS_SECTIONS = (
    "greenhouse",
    "ashby",
    "lever",
    "smartrecruiters",
    "recruitee",
    "teamtailor",
    "workable",
    "personio",
    "workday",
)

_IGNORED_SLUGS = {
    "about",
    "about-us",
    "api",
    "apply",
    "auth",
    "blog",
    "candidate",
    "candidates",
    "careers",
    "company",
    "en",
    "en-us",
    "external",
    "help",
    "home",
    "jobs",
    "login",
    "privacy",
    "pt-br",
    "pt-pt",
    "search",
    "signin",
    "signup",
    "support",
    "terms",
}


def extract_ats_board_from_url(url: str, company_hint: str = "") -> dict[str, Any] | None:
    """Extrai metadados candidatos de um URL de ATS suportado (sem nunca assumir validade antes do probe)."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None

    host = (parsed.netloc or "").casefold().split(":", 1)[0]
    parts = [p for p in (parsed.path or "").split("/") if p]
    company_clean = (company_hint or "").strip()

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io"} and parts:
        slug = parts[0].strip()
        if slug and slug.casefold() not in _IGNORED_SLUGS:
            return {"ats_type": "greenhouse", "slug": slug, "company": company_clean or slug.replace("-", " ").title()}

    if host in {"jobs.ashbyhq.com", "ashbyhq.com"} and parts:
        slug = parts[0].strip()
        if slug and slug.casefold() not in _IGNORED_SLUGS:
            return {"ats_type": "ashby", "slug": slug, "company": company_clean or slug.replace("-", " ").title()}

    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        slug = parts[0].strip()
        if slug and slug.casefold() not in _IGNORED_SLUGS:
            return {"ats_type": "lever", "slug": slug, "company": company_clean or slug.replace("-", " ").title()}

    if host == "jobs.smartrecruiters.com" and parts:
        slug = parts[0].strip()
        if slug and slug.casefold() not in _IGNORED_SLUGS:
            return {"ats_type": "smartrecruiters", "slug": slug, "company": company_clean or slug}

    if host.endswith(".recruitee.com") and host.count(".") >= 2:
        slug = host.split(".", 1)[0]
        if slug and slug not in {"app", "api", "www"}:
            return {"ats_type": "recruitee", "slug": slug, "company": company_clean or slug.title()}

    if host == "apply.workable.com" and parts:
        slug = parts[0].strip()
        if slug and slug.casefold() not in _IGNORED_SLUGS:
            return {"ats_type": "workable", "slug": slug, "company": company_clean or slug.replace("-", " ").title()}

    if host.endswith(".jobs.personio.de") and host.count(".") >= 3:
        slug = host.split(".", 1)[0]
        if slug and slug != "www":
            return {"ats_type": "personio", "slug": slug, "company": company_clean or slug.title()}

    m_wd = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m_wd and parts:
        tenant, wd = m_wd.group(1), m_wd.group(2)
        idx = 1 if (len(parts) >= 2 and re.match(r"^[a-z]{2}(?:-[A-Z]{2})?$", parts[0], re.I)) else 0
        if idx < len(parts):
            site = parts[idx].strip()
            if site and site.casefold() not in {"job", "jobs", "apply"}:
                return {"ats_type": "workday", "tenant": tenant, "wd": wd, "site": site, "company": company_clean or tenant.title()}

    return None


def _board_key(ats_type: str, item: dict[str, Any]) -> str:
    if ats_type == "teamtailor":
        return str(item.get("domain") or "").casefold()
    if ats_type == "workday":
        return f"{item.get('tenant', '')}|{item.get('wd', '')}|{item.get('site', '')}".casefold()
    return str(item.get("slug") or "").casefold()


async def validate_ats_board(board: dict[str, Any], client: httpx.AsyncClient) -> bool:
    """Valida empiricamente (HTTP 200 + schema real) antes de guardar o board descoberto."""
    ats = board.get("ats_type")
    try:
        if ats == "greenhouse":
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board['slug']}/jobs", params={"content": "false"}, timeout=8.0)
            return r.status_code == 200 and isinstance(r.json().get("jobs"), list) and len(r.json()["jobs"]) > 0
        if ats == "ashby":
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board['slug']}", timeout=8.0)
            return r.status_code == 200 and isinstance(r.json().get("jobs"), list) and len(r.json()["jobs"]) > 0
        if ats == "lever":
            r = await client.get(f"https://api.lever.co/v0/postings/{board['slug']}?mode=json", timeout=8.0)
            return r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) > 0
        if ats == "smartrecruiters":
            r = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{board['slug']}/postings",
                params={"country": "pt", "limit": "5"},
                timeout=8.0,
            )
            return r.status_code == 200 and int(r.json().get("totalFound", 0)) > 0
        if ats == "recruitee":
            r = await client.get(f"https://{board['slug']}.recruitee.com/api/offers/", timeout=8.0)
            return r.status_code == 200 and isinstance(r.json().get("offers"), list) and len(r.json()["offers"]) > 0
        if ats == "workable":
            r = await client.post(
                f"https://apply.workable.com/api/v3/accounts/{board['slug']}/jobs",
                json={"query": "", "location": [], "department": [], "worktype": [], "remote": []},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=8.0,
            )
            return r.status_code == 200 and isinstance(r.json().get("results"), list) and len(r.json()["results"]) > 0
        if ats == "personio":
            r = await client.get(f"https://{board['slug']}.jobs.personio.de/xml", timeout=8.0)
            return r.status_code == 200 and "<position>" in r.text
        if ats == "workday":
            url = f"https://{board['tenant']}.{board['wd']}.myworkdayjobs.com/wday/cxs/{board['tenant']}/{board['site']}/jobs"
            r = await client.post(
                url,
                json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": "Portugal"},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=9.0,
            )
            return r.status_code == 200 and int(r.json().get("total", 0)) > 0
    except Exception:
        return False
    return False


def load_discovered_ats(path: Path = DISCOVERED_ATS_PATH) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: list(v) for k, v in data.items() if k in ATS_SECTIONS and isinstance(v, list)}
    except Exception:
        return {}


def save_discovered_ats(data: dict[str, list[dict[str, Any]]], path: Path = DISCOVERED_ATS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def merge_sources_with_discovered(sources_cfg: dict[str, Any], discovered: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    merged = dict(sources_cfg)
    for sec in ATS_SECTIONS:
        base_list = list(sources_cfg.get(sec) or [])
        seen = {_board_key(sec, item) for item in base_list if isinstance(item, dict)}
        for extra in discovered.get(sec) or []:
            if not isinstance(extra, dict):
                continue
            k = _board_key(sec, extra)
            if k and k not in seen:
                seen.add(k)
                base_list.append(extra)
        merged[sec] = base_list
    return merged


async def discover_and_persist_ats(
    urls_with_company: list[tuple[str, str]],
    sources_cfg: dict[str, Any],
    *,
    cache_path: Path = DISCOVERED_ATS_PATH,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Extrai candidatos novos de URLs externos, valida por HTTP 200 e guarda em cache persistente."""
    discovered = load_discovered_ats(cache_path)
    merged = merge_sources_with_discovered(sources_cfg, discovered)

    known: dict[str, set[str]] = {
        sec: {_board_key(sec, item) for item in (merged.get(sec) or []) if isinstance(item, dict)}
        for sec in ATS_SECTIONS
    }

    candidates: list[dict[str, Any]] = []
    seen_cand: set[tuple[str, str]] = set()
    for url, comp in urls_with_company:
        info = extract_ats_board_from_url(url, comp)
        if not info:
            continue
        sec = info["ats_type"]
        k = _board_key(sec, info)
        if not k or k in known.get(sec, set()) or (sec, k) in seen_cand:
            continue
        seen_cand.add((sec, k))
        candidates.append(info)

    if not candidates:
        return []

    added: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(6)

    async def _probe(cand: dict[str, Any], cli: httpx.AsyncClient) -> None:
        async with sem:
            if await validate_ats_board(cand, cli):
                added.append(cand)

    if client is not None:
        await asyncio.gather(*(_probe(c, client) for c in candidates[:20]))
    else:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as own_cli:
            await asyncio.gather(*(_probe(c, own_cli) for c in candidates[:20]))

    if added:
        for item in added:
            sec = item["ats_type"]
            clean_item = {k: v for k, v in item.items() if k != "ats_type"}
            discovered.setdefault(sec, []).append(clean_item)
        save_discovered_ats(discovered, cache_path)

    return added
