"""Scrapers de Trackers Europeus de Estágios (GitHub) e Bolsas de Investigação (EURAXESS Portugal)."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import bs4
import httpx

from estagio_radar.date_utils import parse_publication_datetime, utc_now
from estagio_radar.models import RawJob, SourceHealth
from estagio_radar.scrapers.base import DEFAULT_HEADERS, ScraperBatchResult, retry_async

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _strip_md_bold(text: str) -> str:
    cleaned = MD_LINK_RE.sub(r"\1", text)
    return re.sub(r"[*_`]+", "", cleaned).strip()


def parse_cavlero_markdown(markdown: str, *, now: datetime | None = None) -> list[RawJob]:
    """Extrai linhas da tabela de cavlero/Tech-Internships-Europe."""
    ref = now or utc_now()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    for line in (markdown or "").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| Company") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        company = _strip_md_bold(cells[0])
        title = _strip_md_bold(cells[1])
        location = _strip_md_bold(cells[2])
        link_match = MD_LINK_RE.search(cells[3])
        if not company or not title or not link_match:
            continue
        job_url = link_match.group(2).strip()
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        raw_date = cells[4].strip() if len(cells) >= 5 else ""
        pub_dt = parse_publication_datetime(raw_date, now=ref)
        if pub_dt is None:
            pub_dt = ref - timedelta(days=10)
            raw_date = "Live Tracker"

        low_loc = location.casefold()
        is_rem = "remote" in low_loc or "remoto" in low_loc
        jobs.append(
            RawJob(
                source="EU Tracker (cavlero)",
                source_type="ats",
                job_id=job_url.rstrip("/").split("/")[-1],
                title=title,
                company=company,
                location=location,
                all_locations=[location],
                job_url=job_url,
                modality="Remote" if is_rem else "On-site / Hybrid",
                is_remote=is_rem,
                published_at=pub_dt,
                published_raw=raw_date,
            )
        )
    return jobs


def parse_emjumaev_markdown(markdown: str, *, now: datetime | None = None) -> list[RawJob]:
    """Extrai vagas por empresa de Emjumaev/FAANG-2027-Internships-Tracker-Europe."""
    ref = now or utc_now()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    current_company = ""

    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            heading = _strip_md_bold(stripped.lstrip("#").strip())
            if heading and heading.casefold() not in {"table of contents", "companies", "open internships"}:
                current_company = heading
            continue
        if not stripped.startswith("|") or stripped.startswith("| Role") or stripped.startswith("|---") or not current_company:
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        link_match = MD_LINK_RE.search(cells[0])
        if not link_match:
            continue
        title = link_match.group(1).strip()
        job_url = link_match.group(2).strip()
        if not title or job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        category = _strip_md_bold(cells[1])
        location = _strip_md_bold(cells[3])
        raw_date = cells[4].strip() if len(cells) >= 5 else ""
        is_new = "🆕" in cells[0]
        pub_dt = parse_publication_datetime(raw_date, now=ref)
        if pub_dt is None:
            pub_dt = ref - timedelta(days=3 if is_new else 10)
            raw_date = "Novo" if is_new else "Live Tracker"

        is_rem = "remote" in location.casefold() or "remoto" in location.casefold()
        tags = [category] if category and category != "—" else []
        jobs.append(
            RawJob(
                source="EU Tracker (FAANG)",
                source_type="ats",
                job_id=job_url.rstrip("/").split("/")[-1],
                title=title,
                company=current_company,
                location=location,
                all_locations=[location],
                job_url=job_url,
                modality="Remote" if is_rem else "On-site / Hybrid",
                is_remote=is_rem,
                published_at=pub_dt,
                published_raw=raw_date,
                tags=tags,
            )
        )
    return jobs


async def scrape_github_trackers(trackers: list[dict[str, str]]) -> ScraperBatchResult:
    """Consulta repositórios GitHub europeus de estágios atualizados em tempo real."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for tr in trackers:
            repo = tr["repo"]
            fmt = tr.get("format", "cavlero")
            url = f"https://raw.githubusercontent.com/{repo}/HEAD/README.md"
            try:
                res = await retry_async(client.get, url, timeout=12.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"GitHub {repo}: HTTP {res.status_code}")
                    continue
                ok += 1
                if fmt == "emjumaev":
                    jobs.extend(parse_emjumaev_markdown(res.text))
                else:
                    jobs.extend(parse_cavlero_markdown(res.text))
            except Exception as err:
                failed += 1
                warnings.append(f"GitHub {repo}: {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="GitHub EU Trackers",
            category="ats",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )


def _infer_pt_org_location(org: str, text: str) -> str:
    # Verificar primeiro a organização (evita falsos matches na descrição como "apoio técnico")
    for source_txt in (org, text):
        low = str(source_txt or "").casefold()
        if any(k in low for k in ("porto", "inesctec", "inesc tec", "feup", "iceta", "up.pt")):
            return "Porto, Portugal"
        if any(k in low for k in ("bragança", "braganca", "ipb.pt")):
            return "Bragança, Portugal"
        if any(k in low for k in ("utad", "trás-os-montes", "tras-os-montes", "vila real")):
            return "Vila Real, Portugal"
        if "coimbra" in low:
            return "Coimbra, Portugal"
        if "braga" in low or "minho" in low:
            return "Braga, Portugal"
        if "aveiro" in low:
            return "Aveiro, Portugal"
        if "évora" in low or "evora" in low:
            return "Évora, Portugal"
        if "algarve" in low or "faro" in low:
            return "Faro, Portugal"
        if "leiria" in low:
            return "Leiria, Portugal"
        if "covilhã" in low or "covilha" in low or "beira interior" in low:
            return "Covilhã, Portugal"
        if any(k in low for k in ("setúbal", "setubal", "palmela", "caparica", "fct nova", "fct-nova", "almada")):
            return "Setúbal / Margem Sul, Portugal"
        if any(
            k in low
            for k in (
                "lisboa", "lisbon", "instituto superior técnico", "instituto superior tecnico",
                "ist-id", "inesc-id", "inesc id", "fcul", "iscte", "champalimaud",
                "gulbenkian", "nova sbe", "carcavelos", "oeiras", "taguspark",
            )
        ):
            return "Lisboa, Portugal"
    return "Portugal"


def parse_euraxess_html(html: str, *, now: datetime | None = None) -> list[RawJob]:
    """Extrai Bolsas de Investigação e ofertas académicas em Portugal do portal EURAXESS."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    ref = now or utc_now()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()

    cards = soup.select("article.ecl-content-item") or soup.select("div.views-row") or soup.select("article")
    for card in cards:
        link = card.select_one("h3 a[href*='/jobs/']") or card.select_one("a[href*='/jobs/']")
        if not link:
            continue
        title = link.get_text(" ", strip=True)
        href = str(link.get("href") or "").strip()
        if not title or not href:
            continue
        job_url = urljoin("https://euraxess.ec.europa.eu", href)
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        full_text = card.get_text(" | ", strip=True)
        # Normalmente formato: "Organization | Posted on: 24 November 2026 | Title | Description..."
        org = "Universidade / Centro I&D PT"
        parts = [p.strip() for p in full_text.split("|") if p.strip()]
        if parts and "posted on" not in parts[0].casefold():
            org = parts[0][:120]

        date_match = re.search(r"Posted\s+on:\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", full_text, re.I)
        pub_raw = date_match.group(1) if date_match else ""
        pub_dt = parse_publication_datetime(pub_raw, now=ref)

        loc = _infer_pt_org_location(org, full_text)
        jid = job_url.rstrip("/").split("/")[-1]
        jobs.append(
            RawJob(
                source="EURAXESS Portugal",
                source_type="pt_board",
                job_id=jid,
                title=title,
                company=org,
                location=loc,
                all_locations=[loc],
                job_url=job_url,
                modality="On-site / Hybrid",
                is_remote=False,
                published_at=pub_dt,
                published_raw=pub_raw,
                description=full_text[:1200],
            )
        )
    return jobs


async def scrape_euraxess_pt(entries: list[dict[str, str]]) -> ScraperBatchResult:
    """Recolhe Bolsas de Investigação em IA/Informática em Portugal no EURAXESS."""
    t0 = time.monotonic()
    jobs: list[RawJob] = []
    seen_urls: set[str] = set()
    ok = failed = 0
    warnings: list[str] = []

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for entry in entries:
            url = entry["url"]
            try:
                res = await retry_async(client.get, url, timeout=14.0)
                if res.status_code != 200:
                    failed += 1
                    warnings.append(f"EURAXESS {entry.get('label', url)}: HTTP {res.status_code}")
                    continue
                ok += 1
                for j in parse_euraxess_html(res.text):
                    if j.job_url not in seen_urls:
                        seen_urls.add(j.job_url)
                        jobs.append(j)
            except Exception as err:
                failed += 1
                warnings.append(f"EURAXESS {entry.get('label', url)}: {err}")

    return ScraperBatchResult(
        jobs=jobs,
        health=SourceHealth(
            source="EURAXESS Portugal",
            category="pt_board",
            requests_ok=ok,
            requests_failed=failed,
            raw_count=len(jobs),
            duration_ms=int((time.monotonic() - t0) * 1000),
            warnings=warnings[:5],
        ),
    )
