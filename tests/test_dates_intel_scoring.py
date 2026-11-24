"""Testes de datas precisas, inteligência Teamlyzer e algoritmo de scoring."""

from datetime import datetime, timedelta, timezone

from estagio_radar.company_intel import CompanyIntelService, compute_bayesian_rating
from estagio_radar.date_utils import format_freshness, parse_publication_datetime, recency_check
from estagio_radar.models import CompanyProfile, CompanyType, DomainCategory, GeoZone, RoleTier
from estagio_radar.scoring import compute_score

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def test_date_utils_preserves_minutes_and_hours():
    dt_min = parse_publication_datetime("20 minutes ago", now=NOW)
    assert dt_min == NOW - timedelta(minutes=20)
    assert format_freshness(dt_min, now=NOW) == "Há 20m"

    dt_pt = parse_publication_datetime("No site há 3 horas", now=NOW)
    assert dt_pt == NOW - timedelta(hours=3)
    assert format_freshness(dt_pt, now=NOW) == "Há 3h"

    dt_br = parse_publication_datetime("24-11-2026", now=NOW)
    assert dt_br is not None and dt_br.date().isoformat() == "2026-11-24"

    dt_short = parse_publication_datetime("18 nov", now=NOW)
    assert dt_short is not None and dt_short.month == 11 and dt_short.day == 18

    assert recency_check(NOW - timedelta(days=5), max_days=7, now=NOW)[0]
    assert not recency_check(NOW - timedelta(days=20), max_days=14, now=NOW)[0]


def test_teamlyzer_profile_parses_consultancy_and_salary(tmp_path):
    html_consult = """
    <html><head><link rel="canonical" href="https://pt.teamlyzer.com/companies/prime-it"></head>
    <body>
      <h1 class="reduce-h1">PrimeIT</h1>
      <span itemprop="aggregateRating">
        <meta itemprop="ratingValue" content="3.1">
        <meta itemprop="reviewCount" content="584">
      </span>
      <a href="/companies/ranking/consultoras-outsourcing-it">Consultoria & Outsourcing IT</a>
      <p>Salário médio 30.940€</p>
    </body></html>
    """
    parsed = CompanyIntelService.parse_teamlyzer_profile(
        html_consult, "https://pt.teamlyzer.com/companies/prime-it", "PrimeIT"
    )
    assert parsed is not None
    assert parsed["teamlyzer_rating"] == 3.1
    assert parsed["teamlyzer_reviews"] == 584
    assert "consultoras-outsourcing-it" in parsed["teamlyzer_categories"]
    assert parsed["avg_salary_eur"] == 30940

    svc = CompanyIntelService(cache_path=tmp_path / "cache.json")
    profile = svc._build_profile("PrimeIT", parsed)
    assert profile.is_consultancy is True
    assert profile.company_tier == "CONSULTANCY"
    assert profile.company_type == CompanyType.CONSULTANCY


def test_teamlyzer_rejects_phantom_five_star_zero_reviews():
    phantom = """
    <html><head><link rel="canonical" href="https://pt.teamlyzer.com/companies/fake-co"></head>
    <body>
      <h1 class="reduce-h1">Fake Co</h1>
      <span itemprop="aggregateRating">
        <meta itemprop="ratingValue" content="5.0">
        <meta itemprop="reviewCount" content="0">
      </span>
    </body></html>
    """
    assert CompanyIntelService.parse_teamlyzer_profile(phantom, "https://pt.teamlyzer.com/companies/fake-co", "Fake Co") is None


def test_scoring_product_tech_and_setubal_outranks_consultancy():
    bayes = compute_bayesian_rating(3.5, 38)
    assert bayes is not None and 3.3 < bayes < 3.55

    cf = CompanyProfile(
        company_name="Cloudflare",
        normalized_name="cloudflare",
        teamlyzer_rating=3.5,
        teamlyzer_reviews=38,
        bayesian_rating=bayes,
        avg_salary_eur=64400,
        company_tier="S",
        company_type=CompanyType.PRODUCT_TECH,
        is_consultancy=False,
    )
    consult = CompanyProfile(
        company_name="PrimeIT",
        normalized_name="primeit",
        teamlyzer_rating=3.5,
        teamlyzer_reviews=584,
        bayesian_rating=3.49,
        avg_salary_eur=30940,
        company_tier="CONSULTANCY",
        company_type=CompanyType.CONSULTANCY,
        is_consultancy=True,
    )

    s_cf, bd_cf, badges_cf = compute_score(
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.SETUBAL_LOCAL,
        company=cf,
        published_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    s_cons, bd_cons, badges_cons = compute_score(
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.SETUBAL_LOCAL,
        company=consult,
        published_at=NOW - timedelta(hours=1),
        now=NOW,
    )

    assert s_cf >= 95.0
    assert bd_cons.company_points <= 8.0
    assert s_cf - s_cons >= 18.0
    assert "📍 SETÚBAL" in badges_cf
    assert "⚠️ CONSULTORA" in badges_cons


def test_teamlyzer_direct_meta_and_auto_consultancy(tmp_path):
    # Sem wrapper <span itemprop="aggregateRating"> — tal como The Navigator Company
    nav_html = """
    <html><head><link rel="canonical" href="https://pt.teamlyzer.com/companies/the-navigator-company"></head>
    <body>
      <h1 class="reduce-h1">The Navigator Company</h1>
      <meta itemprop="ratingValue" content="3.1">
      <meta itemprop="reviewCount" content="3">
      <div class="c_rating">2.6</div>
    </body></html>
    """
    parsed = CompanyIntelService.parse_teamlyzer_profile(
        nav_html, "https://pt.teamlyzer.com/companies/the-navigator-company", "The Navigator Company"
    )
    assert parsed is not None
    assert parsed["teamlyzer_rating"] == 3.1
    assert parsed["teamlyzer_reviews"] == 3

    svc = CompanyIntelService(cache_path=tmp_path / "c2.json")
    bout = svc._build_profile("bOut Consulting", None)
    assert bout.is_consultancy is True
    assert bout.company_tier == "CONSULTANCY"

    devo = svc._build_profile("Devoteam | Cyber Trust", None)
    assert devo.is_consultancy is True
