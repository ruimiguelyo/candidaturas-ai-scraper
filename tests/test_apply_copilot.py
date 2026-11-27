"""Testes para o Copiloto de Candidaturas v5.0 (seleção CV, kits, storage, API e Playwright mock)."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from estagio_radar.apply_copilot import (
    detect_apply_strategy,
    extract_application_email,
    generate_application_kit,
    select_best_cv,
)
from estagio_radar.autofill_worker import run_autofill_browser
from estagio_radar.dashboard import make_handler
from estagio_radar.models import (
    CompanyProfile,
    CompanyType,
    DomainCategory,
    EnrichedJob,
    GeoZone,
    RoleTier,
    ScoreBreakdown,
)
from estagio_radar.storage import RadarStorage

NOW = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


def _sample_profile(tmp_path: Path) -> dict:
    for fname in ("Rui_Sa_CV_Applied_AI.pdf", "Rui_Sa_CV_SWE_FullStack.pdf", "Rui_Sa_CV_Enterprise_Tech.pdf"):
        (tmp_path / fname).write_bytes(b"%PDF-1.4 mock")
    return {
        "first_name": "Alex",
        "last_name": "Silva",
        "full_name": "Alex Silva",
        "email": "alex.silva@example.com",
        "phone": "+351 910 000 000",
        "location": "Setúbal, Portugal",
        "linkedin_url": "https://www.linkedin.com/in/alex-silva-example/",
        "github_url": "https://github.com/example-user",
        "portfolio_url": "https://example-user.github.io/",
        "university": "Polytechnic Institute of Setúbal (IPS)",
        "degree": "BSc in Computer Engineering",
        "graduation_year": "2027",
        "cvs": {
            "ai_ml": str(tmp_path / "Rui_Sa_CV_Applied_AI.pdf"),
            "swe": str(tmp_path / "Rui_Sa_CV_SWE_FullStack.pdf"),
            "enterprise": str(tmp_path / "Rui_Sa_CV_Enterprise_Tech.pdf"),
        },
    }


def _make_job(
    *,
    fp: str = "fp-v5-1",
    title: str = "AI Research Intern",
    company: str = "Feedzai",
    domain: DomainCategory = DomainCategory.AI_ML,
    source: str = "Greenhouse (Feedzai)",
    source_type: str = "ats",
    job_url: str = "https://boards.greenhouse.io/feedzai/jobs/1234",
    snippet: str = "",
) -> EnrichedJob:
    return EnrichedJob(
        fingerprint=fp,
        title=title,
        company=company,
        location="Lisboa, Portugal",
        job_url=job_url,
        canonical_url=job_url,
        sources=[source],
        primary_source=source,
        source_type=source_type,
        role_tier=RoleTier.INTERNSHIP,
        domain=domain,
        geo_zone=GeoZone.LISBOA_METRO,
        published_at=NOW,
        published_date="2026-11-27",
        freshness_label="Há 1h",
        description_snippet=snippet,
        company_profile=CompanyProfile(
            company_name=company,
            normalized_name=company.lower(),
            teamlyzer_rating=4.0,
            teamlyzer_reviews=25,
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=92.0,
        score_breakdown=ScoreBreakdown(
            total=92.0,
            role_points=30,
            company_points=25,
            location_points=17,
            freshness_points=20,
        ),
        badges=["🔥 NOVO", "⚡ ATS DIRETO"],
    )


def test_select_best_cv_routing_and_override(tmp_path):
    profile = _sample_profile(tmp_path)

    ai_job = _make_job(domain=DomainCategory.AI_ML)
    assert select_best_cv(ai_job, profile)["track"] == "ai_ml"

    data_job = _make_job(domain=DomainCategory.DATA, title="Data Science Intern")
    assert select_best_cv(data_job, profile)["track"] == "ai_ml"

    swe_job = _make_job(domain=DomainCategory.SWE, title="Software Engineer Intern")
    assert select_best_cv(swe_job, profile)["track"] == "swe"

    sap_job = _make_job(domain=DomainCategory.SWE, title="SAP ABAP S/4HANA Junior Developer")
    assert select_best_cv(sap_job, profile)["track"] == "enterprise"

    manual = select_best_cv(swe_job, profile, override_track="ai_ml")
    assert manual["track"] == "ai_ml"
    assert manual["exists"] is True


def test_detect_apply_strategy_and_email_extraction():
    gh_job = _make_job(job_url="https://boards.greenhouse.io/cloudflare/jobs/99", source_type="ats")
    assert detect_apply_strategy(gh_job)["type"] == "ats_autofill"

    eur_job = _make_job(
        job_url="https://euraxess.ec.europa.eu/jobs/482969",
        source="EURAXESS Portugal",
        source_type="pt_board",
    )
    assert detect_apply_strategy(eur_job)["type"] == "email_or_portal"

    li_job = _make_job(
        job_url="https://pt.linkedin.com/jobs/view/ai-intern-444",
        source="LinkedIn",
        source_type="linkedin",
    )
    assert detect_apply_strategy(li_job)["type"] == "linkedin_portal"

    found = extract_application_email(
        "https://euraxess.ec.europa.eu/jobs/482969",
        "Send applications to info@euraxess.ec.europa.eu or bolsas@inesc-id.pt before deadline.",
        fetch_page=False,
    )
    assert found == "bolsas@inesc-id.pt"


def test_generate_application_kit_languages_refs_and_honest_deloitte(tmp_path):
    profile = _sample_profile(tmp_path)

    pt_grant = _make_job(
        fp="fp-inesc",
        title="Bolsa de Investigação - BI|2026/975 Projeto SHELL",
        company="INESC ID",
        domain=DomainCategory.AI_ML,
        source="EURAXESS Portugal",
        source_type="pt_board",
        job_url="https://euraxess.ec.europa.eu/jobs/482969",
        snippet="Enviar candidatura para candidaturas@inesc-id.pt com CV.",
    )
    kit_pt = generate_application_kit(pt_grant, profile, fetch_email=False)
    assert kit_pt["language"] == "pt"
    assert "BI|2026/975" in kit_pt["email_subject"]
    assert kit_pt["detected_email"] == "candidaturas@inesc-id.pt"
    assert "Python, Java e PyTorch" in kit_pt["short_cover_note"]
    assert "Apex" not in kit_pt["short_cover_note"]
    assert "linkedin.com/search/results/people/" in kit_pt["recruiter_search_url"]

    en_job = _make_job(title="Software Engineer Intern (2027)", company="Cloudflare", domain=DomainCategory.SWE)
    kit_en = generate_application_kit(en_job, profile, fetch_email=False)
    assert kit_en["language"] == "en"
    assert kit_en["cv"]["track"] == "swe"
    assert "Cloudflare" in kit_en["short_cover_note"]
    assert "PyTorch" in kit_en["short_cover_note"]


def test_storage_get_job_and_dashboard_copilot_endpoints(tmp_path):
    profile = _sample_profile(tmp_path)
    profile_path = tmp_path / "candidate_profile.yaml"
    import yaml

    profile_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")

    db = RadarStorage(tmp_path / "radar.db")
    job = _make_job(fp="fp-http-1")
    db.upsert_jobs([job], now=NOW)

    assert db.get_job("missing") is None
    loaded = db.get_job("fp-http-1")
    assert loaded is not None and loaded.company == "Feedzai"

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, profile_path=profile_path))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        prof_resp = json.loads(urllib.request.urlopen(f"{base}/api/profile", timeout=5).read().decode("utf-8"))
        assert prof_resp["configured"] is True
        assert prof_resp["email"] == "alex.silva@example.com"
        assert len(prof_resp["cvs"]) == 3

        kit_resp = json.loads(
            urllib.request.urlopen(f"{base}/api/apply-kit?fingerprint=fp-http-1", timeout=5).read().decode("utf-8")
        )
        assert kit_resp["cv"]["track"] == "ai_ml"
        assert kit_resp["company"] == "Feedzai"
        assert "JudgeShift CX" in "\n".join(kit_resp["highlight_bullets"])
    finally:
        server.shutdown()
        server.server_close()


def test_playwright_autofill_smoke_on_local_mock_form(tmp_path):
    pytest.importorskip("playwright")
    profile = _sample_profile(tmp_path)
    job = _make_job(fp="fp-pw-1", domain=DomainCategory.AI_ML)
    kit = generate_application_kit(job, profile, fetch_email=False)

    mock_html = tmp_path / "mock_ats.html"
    mock_html.write_text(
        """<!doctype html>
        <html><body>
          <form id="app-form">
            <label for="first_name">First Name</label>
            <input id="first_name" name="first_name" type="text">
            <label for="last_name">Last Name</label>
            <input id="last_name" name="last_name" type="text">
            <label for="email">Email</label>
            <input id="email" name="email" type="email">
            <label for="phone">Phone</label>
            <input id="phone" name="phone" type="tel">
            <label for="linkedin">LinkedIn Profile</label>
            <input id="linkedin" name="linkedin" type="url">
            <label for="github">GitHub</label>
            <input id="github" name="github" type="url">
            <label for="resume">Resume / CV</label>
            <input id="resume" name="resume" type="file">
            <label for="cover">Cover Letter / Additional Info</label>
            <textarea id="cover" name="cover_letter"></textarea>
            <button type="submit">Submit Application</button>
          </form>
        </body></html>""",
        encoding="utf-8",
    )

    res = run_autofill_browser(
        job,
        kit,
        profile,
        headless=True,
        keep_open=False,
        target_url=mock_html.as_uri(),
    )
    assert res["ok"] is True
    assert res["cv_attached"] is True
    assert {"first_name", "last_name", "email", "phone", "linkedin_url", "github_url", "cover_note", "cv_pdf"}.issubset(
        set(res["filled_fields"])
    )
