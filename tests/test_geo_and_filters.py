"""Testes unitários para classificação geográfica e motor rigoroso de filtragem."""

from datetime import datetime, timezone

from estagio_radar.filter_engine import evaluate_raw_job
from estagio_radar.geo import classify_geo
from estagio_radar.models import DomainCategory, GeoZone, RawJob, RoleTier


NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def _job(**kwargs) -> RawJob:
    defaults = {
        "source": "LinkedIn",
        "source_type": "linkedin",
        "job_id": "123",
        "title": "Software Engineer Intern",
        "company": "Cloudflare",
        "location": "Lisbon, Portugal",
        "job_url": "https://example.com/job/123",
        "published_at": NOW,
    }
    defaults.update(kwargs)
    return RawJob(**defaults)


def test_geo_setubal_and_margem_sul_priority():
    for loc in (
        "Setúbal, Setúbal, Portugal",
        "Palmela, Portugal",
        "Almada, Margem Sul",
        "Seixal, Portugal",
        "Autoeuropa, Quinta do Anjo",
    ):
        zone, _ = classify_geo(loc)
        assert zone == GeoZone.SETUBAL_LOCAL, f"Esperado SETUBAL_LOCAL para {loc}"


def test_geo_lisboa_metro():
    for loc in ("Lisbon, Portugal", "Oeiras, Lisbon, Portugal", "Taguspark, Porto Salvo", "Albarraque, Sintra"):
        zone, _ = classify_geo(loc)
        assert zone == GeoZone.LISBOA_METRO


def test_geo_rejects_us_india_and_non_remote_porto():
    assert classify_geo("United States", is_remote=True)[0] == GeoZone.REJECTED_GEO
    assert classify_geo("Bengaluru, India")[0] == GeoZone.REJECTED_GEO
    assert classify_geo("Remote - United States", is_remote=True)[0] == GeoZone.REJECTED_GEO
    assert classify_geo("Porto, Portugal", is_remote=False)[0] == GeoZone.REJECTED_GEO
    assert classify_geo("Aveiro, Portugal", is_remote=False)[0] == GeoZone.REJECTED_GEO
    # Mas remoto europeu/global legítimo passa!
    assert classify_geo("Home based - EMEA", is_remote=True)[0] == GeoZone.REMOTE_PT
    assert classify_geo("Remote - Portugal", is_remote=True)[0] == GeoZone.REMOTE_PT
    # Descrição com restrição estrita aos EUA rejeita mesmo se disser "Remote"
    assert classify_geo(
        "Worldwide",
        is_remote=True,
        description="Candidates must be authorized to work in the United States.",
    )[0] == GeoZone.REJECTED_GEO


def test_filter_blocks_internal_and_international_false_positives():
    for bad_title in (
        "Senior Internal Auditor",
        "International Payroll Analyst",
        "Staff Software Engineer, Internal Products",
        "International Tax Manager",
        "Internal Communications Specialist",
    ):
        dec = evaluate_raw_job(_job(title=bad_title, location="Remote - Europe", is_remote=True), now=NOW)
        assert not dec.accepted, f"Falso positivo passou: {bad_title}"


def test_filter_blocks_bare_mid_senior_titles_without_intern_or_junior():
    for title in ("Data Scientist", "Backend Developer", "Java Developer", "AI Engineer"):
        dec = evaluate_raw_job(_job(title=title), now=NOW)
        assert not dec.accepted
        assert dec.reason_code == "not_internship_or_entry_level"


def test_filter_rejects_deloitte_and_non_tech_interns():
    assert evaluate_raw_job(_job(company="Deloitte Portugal", title="AI Intern"), now=NOW).reason_code == "excluded_company"
    assert not evaluate_raw_job(_job(title="People Operations Intern"), now=NOW).accepted
    assert not evaluate_raw_job(_job(title="Estágios Eng. Civil", location="Setúbal, Portugal"), now=NOW).accepted
    assert not evaluate_raw_job(_job(title="Sales Analytics Intern"), now=NOW).accepted


def test_filter_rejects_fake_junior_requiring_4_years():
    dec = evaluate_raw_job(
        _job(
            title="Junior Python Developer",
            description="Requirements: minimum of 4+ years of experience with Python and microservices.",
        ),
        now=NOW,
    )
    assert not dec.accepted
    assert dec.reason_code == "requires_mid_senior_experience"


def test_filter_accepts_real_internships_and_classifies_domains():
    dec1 = evaluate_raw_job(
        _job(
            company="The Navigator Company",
            title="BEaPRO | Estágios Profissionais - Especialista em Cibersegurança",
            location="Setúbal, Setúbal, Portugal",
        ),
        now=NOW,
    )
    assert dec1.accepted
    assert dec1.role_tier == RoleTier.INTERNSHIP
    assert dec1.domain == DomainCategory.CYBER
    assert dec1.geo_zone == GeoZone.SETUBAL_LOCAL

    dec2 = evaluate_raw_job(
        _job(
            company="Bose Corporation",
            title="Data Science Co-Op (NLP & GenAI)",
            location="Lisbon, Portugal",
        ),
        now=NOW,
    )
    assert dec2.accepted
    assert dec2.role_tier == RoleTier.INTERNSHIP
    assert dec2.domain == DomainCategory.AI_ML
    assert dec2.geo_zone == GeoZone.LISBOA_METRO

    dec3 = evaluate_raw_job(
        _job(
            company="Zurich Insurance",
            title="Data Analytic - Zurich Graduate",
            location="Lisboa, Lisbon, Portugal",
        ),
        now=NOW,
    )
    assert dec3.accepted
    assert dec3.domain == DomainCategory.DATA
