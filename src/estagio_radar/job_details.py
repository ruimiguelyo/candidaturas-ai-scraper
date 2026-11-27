"""Recolha on-demand da descrição completa da vaga e do URL oficial externo de candidatura."""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import bs4
import httpx

from estagio_radar.models import EnrichedJob
from estagio_radar.scrapers.base import clean_html_text

logger = logging.getLogger("estagio_radar.job_details")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IGNORED_EMAIL_FRAGMENTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "privacy@",
    "gdpr@",
    "dpo@",
    "euraxess",
    "ec.europa.eu",
    "example.com",
    "linkedin.com",
    "sentry.io",
    "wixpress.com",
)
LINKEDIN_ID_RE = re.compile(r"(?:-|/jobs/view/(?:[^/?]+-)?|currentJobId=)(\d{7,12})(?:[/?&#]|$)")
GREENHOUSE_URL_RE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I)
SMARTRECRUITERS_URL_RE = re.compile(r"jobs\.smartrecruiters\.com/([^/]+)/([^/?#]+)", re.I)


def _filter_email(text: str) -> str | None:
    for candidate in EMAIL_RE.findall(text or ""):
        low = candidate.casefold().strip()
        if any(bad in low for bad in IGNORED_EMAIL_FRAGMENTS):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".css", ".js")):
            continue
        return candidate.strip()
    return None


def _unwrap_linkedin_redirect(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if "/redir/redirect" in parsed.path or "url=" in parsed.query:
        qs = parse_qs(parsed.query)
        target = (qs.get("url") or [""])[0]
        if target:
            return unquote(target).strip()
    return raw


def parse_linkedin_detail_html(html: str) -> tuple[str, str | None]:
    """Extrai texto completo do anúncio LinkedIn Guest e o URL de candidatura externa se existir."""
    soup = bs4.BeautifulSoup(html or "", "lxml")
    desc_el = (
        soup.select_one(".show-more-less-html__markup")
        or soup.select_one(".description__text")
        or soup.select_one("[class*='description']")
    )
    desc_text = ""
    if desc_el:
        for br in desc_el.find_all(["br", "li", "p"]):
            br.insert_before("\n")
        desc_text = re.sub(r"[ \t]+", " ", desc_el.get_text(" ", strip=False))
        desc_text = re.sub(r"\n{3,}", "\n\n", desc_text).strip()

    ext_url: str | None = None
    apply_code = soup.select_one("code#applyUrl")
    if apply_code:
        raw_code = apply_code.get_text(" ", strip=True) or "".join(str(c) for c in apply_code.contents)
        m = re.search(r"https?://[^\s\"'<>]+", raw_code)
        if m:
            ext_url = _unwrap_linkedin_redirect(m.group(0))

    if not ext_url:
        redir_link = soup.select_one("a[href*='/redir/redirect']")
        if redir_link and redir_link.get("href"):
            candidate = _unwrap_linkedin_redirect(str(redir_link["href"]))
            if candidate.startswith("http") and "linkedin.com" not in candidate.casefold():
                ext_url = candidate

    if ext_url and "linkedin.com" in ext_url.casefold():
        ext_url = None
    return desc_text[:4500], ext_url


def parse_greenhouse_detail_json(payload: dict[str, Any]) -> str:
    """Extrai descrição completa limpa de um endpoint Greenhouse Job Detail."""
    raw = html_lib.unescape(str(payload.get("content") or ""))
    return clean_html_text(raw)[:4500]


def parse_smartrecruiters_detail_json(payload: dict[str, Any]) -> str:
    """Extrai secções estruturadas do JSON da SmartRecruiters."""
    sections = ((payload.get("jobAd") or {}).get("sections")) or (
        (payload.get("defaultJobAd") or {}).get("sections")
    ) or {}
    parts: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key)
        if isinstance(sec, dict) and sec.get("text"):
            title = str(sec.get("title") or key).strip()
            body = clean_html_text(html_lib.unescape(str(sec["text"])))[:2000]
            if body:
                parts.append(f"{title}: {body}")
    return "\n\n".join(parts)[:4500]


def fetch_job_details(job: EnrichedJob) -> dict[str, str | None]:
    """Vai buscar a descrição completa da vaga e eventual URL externo / email de candidatura."""
    url = (job.job_url or "").strip()
    fallback_desc = (job.description_snippet or "").strip()
    if not url:
        return {"description": fallback_desc, "external_apply_url": None, "detected_email": _filter_email(fallback_desc)}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    }

    try:
        low_url = url.casefold()
        if "linkedin.com" in low_url:
            m = LINKEDIN_ID_RE.search(url)
            if m:
                jid = m.group(1)
                guest_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
                try:
                    from curl_cffi import requests as creq

                    res = creq.get(guest_url, impersonate="chrome", timeout=10)
                    status = res.status_code
                    html = res.text
                except ImportError:
                    res = httpx.get(guest_url, headers=headers, follow_redirects=True, timeout=10.0)
                    status = res.status_code
                    html = res.text
                if status == 200 and html:
                    desc, ext_url = parse_linkedin_detail_html(html)
                    return {
                        "description": desc or fallback_desc,
                        "external_apply_url": ext_url,
                        "detected_email": _filter_email(desc),
                    }
            return {
                "description": fallback_desc,
                "external_apply_url": None,
                "detected_email": _filter_email(fallback_desc),
            }

        gh_m = GREENHOUSE_URL_RE.search(url)
        if gh_m:
            slug, jid = gh_m.group(1), gh_m.group(2)
            res = httpx.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}",
                headers=headers,
                follow_redirects=True,
                timeout=8.0,
            )
            if res.status_code == 200:
                desc = parse_greenhouse_detail_json(res.json())
                return {
                    "description": desc or fallback_desc,
                    "external_apply_url": None,
                    "detected_email": _filter_email(desc),
                }

        sr_m = SMARTRECRUITERS_URL_RE.search(url)
        if sr_m:
            slug, jid = sr_m.group(1), sr_m.group(2)
            res = httpx.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}",
                headers=headers,
                follow_redirects=True,
                timeout=8.0,
            )
            if res.status_code == 200:
                desc = parse_smartrecruiters_detail_json(res.json())
                return {
                    "description": desc or fallback_desc,
                    "external_apply_url": None,
                    "detected_email": _filter_email(desc),
                }

        res = httpx.get(url, headers=headers, follow_redirects=True, timeout=8.0)
        if res.status_code == 200 and res.text:
            soup = bs4.BeautifulSoup(res.text, "lxml")
            mailto = soup.select_one("a[href^='mailto:']")
            mailto_email = None
            if mailto and mailto.get("href"):
                mailto_email = _filter_email(str(mailto["href"]).replace("mailto:", "").split("?")[0])

            for noise in soup.select("script, style, nav, footer, header"):
                noise.decompose()
            container = (
                soup.select_one("article")
                or soup.select_one("main")
                or soup.select_one("[class*='description'], [id*='description']")
                or soup.body
            )
            txt = re.sub(r"\s+", " ", container.get_text(" ", strip=True)) if container else ""
            best_desc = txt[:4500] if len(txt) > len(fallback_desc) else fallback_desc
            return {
                "description": best_desc,
                "external_apply_url": None,
                "detected_email": mailto_email or _filter_email(txt) or _filter_email(fallback_desc),
            }
    except Exception as err:
        logger.debug("Falha ao obter detalhes de %s: %s", url, err)

    return {
        "description": fallback_desc,
        "external_apply_url": None,
        "detected_email": _filter_email(fallback_desc),
    }
