"""Deduplicação inteligente em 2 fases (URL canónica + identidade cruzada entre fontes)."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from estagio_radar.company_intel import base_company_name, normalize_company_key, strip_accents
from estagio_radar.models import FilterDecision, GeoZone, RawJob

TRACKING_QUERY_PARAMS = {
    "campaign", "eid", "lipi", "midtoken", "ref", "refid", "source", "src",
    "trk", "trackingid", "gh_src", "lever-source", "lever-via", "fbclid", "gclid",
}

SOURCE_PRIORITY = {
    "ats": 4,
    "pt_board": 3,
    "linkedin": 2,
    "remote_eu": 1,
}


def canonicalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.netloc:
            return raw
        clean_qs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.casefold().startswith("utm_")
            and k.casefold() not in TRACKING_QUERY_PARAMS
        ]
        # Greenhouse usa /jobs/<id>?gh_jid=<id> — se <id> já está no path, remove gh_jid redundante
        if "greenhouse.io" in parsed.netloc.casefold() and re.search(r"/jobs/\d+$", parsed.path):
            clean_qs = [(k, v) for k, v in clean_qs if k.casefold() != "gh_jid"]
        return urlunsplit((
            parsed.scheme.lower() or "https",
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            urlencode(sorted(clean_qs)),
            "",
        ))
    except ValueError:
        return raw


def normalize_title_core(title: str) -> str:
    ascii_title = strip_accents(title).casefold()
    # Remove sufixos como "(f/m/div.)", "(m/f/d)", "- Lisbon, PT", "[2027]"
    cleaned = re.sub(r"\((?:f/m/d(?:iv)?\.?|m/f(?:/[a-z]+)?|h/m/x|all genders)\)", " ", ascii_title)
    cleaned = re.sub(r"[-–—]\s*(?:lisbon|lisboa|setubal|portugal|remote|hybrid|pt)\b.*$", " ", cleaned)
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    return " ".join(tokens)


def compute_fingerprint(company: str, title: str, geo_zone: GeoZone) -> str:
    comp_key = normalize_company_key(base_company_name(company))
    title_core = normalize_title_core(title)
    # Normaliza Setúbal/Lisboa como on-site PT vs Remote PT para evitar duplicar vagas multi-cidade
    geo_bucket = "remote" if geo_zone == GeoZone.REMOTE_PT else "pt"
    raw_identity = f"{comp_key}::{title_core}::{geo_bucket}"
    return hashlib.sha1(raw_identity.encode("utf-8")).hexdigest()[:16]


def deduplicate_accepted(
    accepted: list[tuple[RawJob, FilterDecision]],
) -> list[dict]:
    """Consolida variantes da mesma vaga, dando prioridade a ATS direto sobre LinkedIn."""
    # Fase 1: agrupar por URL canónica
    by_url: dict[str, list[tuple[RawJob, FilterDecision]]] = {}
    for job, dec in accepted:
        c_url = canonicalize_url(job.job_url)
        by_url.setdefault(c_url or f"id:{job.source}:{job.job_id}", []).append((job, dec))

    # Fase 2: agrupar por identidade semântica (empresa + título + zona)
    by_fp: dict[str, list[tuple[RawJob, FilterDecision]]] = {}
    for items in by_url.values():
        rep_job, rep_dec = items[0]
        fp = compute_fingerprint(rep_job.company, rep_job.title, rep_dec.geo_zone)
        by_fp.setdefault(fp, []).extend(items)

    merged: list[dict] = []
    for fp, variants in by_fp.items():
        # Escolher o melhor representante: ATS > PT Board > LinkedIn > Remote EU, depois mais metadados
        def rank_variant(pair: tuple[RawJob, FilterDecision]) -> tuple[int, int, int, int]:
            j, _ = pair
            src_prio = SOURCE_PRIORITY.get(j.source_type, 0)
            has_desc = len(j.description or "")
            has_exact_dt = 1 if j.published_at else 0
            has_salary = 1 if j.salary else 0
            return (src_prio, has_salary, has_exact_dt, has_desc)

        best_job, best_dec = max(variants, key=rank_variant)
        sources = list(dict.fromkeys([best_job.source] + sorted({j.source for j, _ in variants})))

        # Escolher melhor zona se uma variante confirmar Setúbal ou Lisboa
        zone_prio = {
            GeoZone.SETUBAL_LOCAL: 4,
            GeoZone.LISBOA_METRO: 3,
            GeoZone.REMOTE_PT: 2,
            GeoZone.PT_UNSPECIFIED: 1,
            GeoZone.REJECTED_GEO: 0,
        }
        best_zone = max((d.geo_zone for _, d in variants), key=lambda z: zone_prio.get(z, 0))
        best_role = min(
            (d.role_tier for _, d in variants if d.role_tier is not None),
            key=lambda r: 0 if r.value == "INTERNSHIP" else 1,
        )
        best_domain = best_dec.domain

        # Data mais antiga verificável entre variantes (indica quando abriu originalmente)
        dates = [j.published_at for j, _ in variants if j.published_at is not None]
        best_date = min(dates) if dates else best_job.published_at

        # União de tags
        all_tags: list[str] = []
        for j, _ in variants:
            for t in j.tags:
                if t and t not in all_tags:
                    all_tags.append(t)

        # Melhor salário / descrição / applicant_count / teamlyzer_hint
        best_salary = best_job.salary or next((j.salary for j, _ in variants if j.salary), None)
        best_desc = max((j.description or "" for j, _ in variants), key=len)
        best_app = next((j.applicant_count for j, _ in variants if j.applicant_count is not None), None)
        best_hint = next((j.teamlyzer_hint for j, _ in variants if j.teamlyzer_hint), None)

        merged.append({
            "fingerprint": fp,
            "job": best_job.model_copy(
                update={
                    "salary": best_salary,
                    "description": best_desc,
                    "tags": all_tags[:12],
                    "published_at": best_date,
                    "applicant_count": best_app,
                    "teamlyzer_hint": best_hint,
                }
            ),
            "canonical_url": canonicalize_url(best_job.job_url),
            "sources": sources,
            "role_tier": best_role,
            "domain": best_domain,
            "geo_zone": best_zone,
            "merged_count": len(variants),
        })

    return merged
