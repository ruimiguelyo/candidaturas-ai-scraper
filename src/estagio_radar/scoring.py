"""Modelo de scoring transparente (0-100) focado em Estágios, Empresas de Produto, Setúbal/Lisboa e Frescura."""

from __future__ import annotations

from datetime import datetime
import re

from estagio_radar.date_utils import ensure_utc, format_freshness, utc_now

_CV_STACK_PATTERNS = (
    ("python", r"\bpython\b"),
    ("pytorch", r"\bpytorch\b"),
    ("jax", r"\bjax\b"),
    ("flax", r"\bflax\b"),
    ("llm", r"\bllms?\b"),
    ("genai", r"\bgen[- ]?ai\b"),
    ("ml", r"\bmachine\s+learning\b"),
    ("dl", r"\bdeep\s+learning\b"),
    ("nlp", r"\bnlp\b"),
    ("typescript", r"\btypescript\b"),
    ("react", r"\breact\b"),
    ("sql", r"\bsql\b|\bpostgresql\b"),
    ("fastapi", r"\bfastapi\b"),
    ("docker", r"\bdocker\b"),
    ("sap", r"\bsap\b|\babap\b"),
)
from estagio_radar.models import (
    CompanyProfile,
    CompanyType,
    DomainCategory,
    GeoZone,
    RoleTier,
    ScoreBreakdown,
)


def compute_score(
    *,
    role_tier: RoleTier,
    domain: DomainCategory,
    geo_zone: GeoZone,
    company: CompanyProfile,
    published_at: datetime | None,
    applicant_count: int | None = None,
    now: datetime | None = None,
    primary_source: str = "",
    source_type: str = "",
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> tuple[float, ScoreBreakdown, list[str]]:
    """Calcula pontuação 0-100 e etiquetas visuais para uma vaga qualificada."""
    notes: list[str] = []
    reference = ensure_utc(now or utc_now())

    # 1. Papel & Domínio (0-30 pts)
    if role_tier == RoleTier.INTERNSHIP:
        role_pts = 24.0
        if domain in {DomainCategory.AI_ML, DomainCategory.DATA}:
            role_pts += 6.0
        elif domain == DomainCategory.SWE:
            role_pts += 4.0
        else:
            role_pts += 3.0
        notes.append(f"Estágio/Trainee ({domain.value}): {role_pts:.0f}/30")
    else:
        role_pts = 14.0
        if domain in {DomainCategory.AI_ML, DomainCategory.DATA}:
            role_pts += 4.0
        elif domain == DomainCategory.SWE:
            role_pts += 2.0
        else:
            role_pts += 1.0
        notes.append(f"Entry/Junior ({domain.value}): {role_pts:.0f}/30")

    # 2. Qualidade da Empresa (0-30 pts) — Consultoras penalizadas no máximo em 8 pts!
    bayes = company.bayesian_rating
    salary = company.avg_salary_eur

    if company.is_consultancy or company.company_tier == "CONSULTANCY":
        comp_pts = min(8.0, 4.0 + (bayes or 2.8))
        notes.append(f"Consultora/Outsourcing (teto 8): {comp_pts:.1f}/30")
    elif company.company_tier == "S":
        comp_pts = 25.0
        if bayes is not None:
            comp_pts += max(0.0, (bayes - 3.0) * 2.5)
        if salary and salary >= 55000:
            comp_pts += 2.0
        comp_pts = min(30.0, comp_pts)
        notes.append(f"Empresa Tier S: {comp_pts:.1f}/30")
    elif company.company_tier == "A":
        comp_pts = 19.0
        if bayes is not None:
            comp_pts += max(0.0, (bayes - 2.8) * 2.5)
        if salary and salary >= 50000:
            comp_pts += 2.0
        comp_pts = min(25.0, comp_pts)
        notes.append(f"Empresa Tier A: {comp_pts:.1f}/30")
    elif company.company_type == CompanyType.PRODUCT_TECH:
        comp_pts = 16.0
        if bayes is not None:
            comp_pts += max(0.0, (bayes - 2.8) * 3.0)
        if salary and salary >= 55000:
            comp_pts += 4.0
        elif salary and salary >= 40000:
            comp_pts += 2.0
        comp_pts = min(24.0, comp_pts)
        notes.append(f"Empresa Produto (Teamlyzer): {comp_pts:.1f}/30")
    elif bayes is not None:
        comp_pts = min(19.0, 10.0 + bayes * 2.0)
        notes.append(f"Empresa Teamlyzer ({bayes:.2f}★): {comp_pts:.1f}/30")
    else:
        comp_pts = 12.0
        notes.append("Empresa sem rating Teamlyzer: 12.0/30")

    # 3. Localização centrada em Setúbal -> Lisboa -> Remoto PT (0-20 pts)
    loc_map = {
        GeoZone.SETUBAL_LOCAL: 20.0,
        GeoZone.REMOTE_PT: 18.0,
        GeoZone.LISBOA_METRO: 17.0,
        GeoZone.PT_UNSPECIFIED: 12.0,
        GeoZone.PT_OTHER_CITY: 10.0,
        GeoZone.REJECTED_GEO: 0.0,
    }
    loc_pts = loc_map.get(geo_zone, 0.0)
    notes.append(f"Zona {geo_zone.value}: {loc_pts:.0f}/20")

    # 4. Frescura / First-Mover Advantage (0-20 pts)
    fresh_pts = 1.0
    hours_old: float | None = None
    if published_at is not None:
        dt = ensure_utc(published_at)
        hours_old = max(0.0, (reference - dt).total_seconds() / 3600.0)
        if hours_old < 3:
            fresh_pts = 20.0
        elif hours_old < 12:
            fresh_pts = 17.0
        elif hours_old < 24:
            fresh_pts = 14.0
        elif hours_old < 48:
            fresh_pts = 11.0
        elif hours_old < 96:
            fresh_pts = 7.0
        elif hours_old < 168:
            fresh_pts = 4.0
        else:
            fresh_pts = 2.0

    if applicant_count is not None and applicant_count < 25:
        fresh_pts = min(20.0, fresh_pts + 2.0)
        notes.append(f"Frescura ({format_freshness(published_at, reference)}, <25 cand.): {fresh_pts:.0f}/20")
    else:
        notes.append(f"Frescura ({format_freshness(published_at, reference)}): {fresh_pts:.0f}/20")

    stack_blob = f"{title} {description} {' '.join(tags or [])}".casefold()
    stack_hits = [label for label, pat in _CV_STACK_PATTERNS if stack_blob.strip() and re.search(pat, stack_blob)]
    stack_bonus = min(4.0, float(len(stack_hits))) if len(stack_hits) >= 2 else 0.0
    if stack_bonus > 0:
        notes.append(f"Match CV ({', '.join(stack_hits[:4])}): +{stack_bonus:.0f}")

    total = round(min(100.0, role_pts + comp_pts + loc_pts + fresh_pts + stack_bonus), 1)
    breakdown = ScoreBreakdown(
        total=total,
        role_points=round(role_pts, 1),
        company_points=round(comp_pts, 1),
        location_points=round(loc_pts, 1),
        freshness_points=round(fresh_pts, 1),
        notes=notes,
    )

    badges: list[str] = []
    if stack_bonus > 0:
        badges.append("🎯 STACK MATCH")
    if hours_old is not None and hours_old < 12:
        badges.append("🔥 NOVO")
    if geo_zone == GeoZone.SETUBAL_LOCAL:
        badges.append("📍 SETÚBAL")
    elif geo_zone == GeoZone.LISBOA_METRO:
        badges.append("🚆 LISBOA")
    elif geo_zone == GeoZone.REMOTE_PT:
        badges.append("🌐 REMOTO PT")
    elif geo_zone == GeoZone.PT_OTHER_CITY:
        badges.append("🇵🇹 PORTUGAL (OUTRA)")
    else:
        badges.append("🇵🇹 PORTUGAL")

    if company.is_consultancy:
        badges.append("⚠️ CONSULTORA")
    elif company.company_tier == "S":
        badges.append("🏆 TIER S")
    elif company.company_tier == "A":
        badges.append("⭐ TIER A")
    elif company.company_type == CompanyType.PRODUCT_TECH:
        badges.append("🏢 PRODUTO")

    domain_badge = {
        DomainCategory.AI_ML: "🤖 IA/ML",
        DomainCategory.DATA: "📊 DADOS",
        DomainCategory.SWE: "💻 SWE",
        DomainCategory.CYBER: "🔐 CIBER",
        DomainCategory.CLOUD_DEVOPS: "☁️ CLOUD",
        DomainCategory.EMBEDDED: "⚙️ EMBEDDED",
    }[domain]
    badges.append(domain_badge)
    badges.append("🎓 ESTÁGIO" if role_tier == RoleTier.INTERNSHIP else "🚀 ENTRY/JR")
    if "EU Tracker" in primary_source:
        badges.append("🎯 EU TRACKER")
    elif "EURAXESS" in primary_source:
        badges.append("🔬 BOLSA R&D")
    elif source_type == "ats":
        badges.append("⚡ ATS DIRETO")

    return total, breakdown, badges
