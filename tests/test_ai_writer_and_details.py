"""Testes do v5.1: redação natural, Gemini Interactions API, parsers de detalhes, extensão CORS e Playwright v2."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from estagio_radar.ai_writer import generate_copy_with_gemini, parse_natural_role_phrase
from estagio_radar.apply_copilot import generate_application_kit
from estagio_radar.autofill_worker import run_autofill_browser
from estagio_radar.dashboard import make_handler
from estagio_radar.job_details import (
    parse_greenhouse_detail_json,
    parse_linkedin_detail_html,
    parse_smartrecruiters_detail_json,
)
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

NOW = datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc)


def _sample_profile(tmp_path: Path) -> dict:
    for fname in ("Rui_Sa_CV_Applied_AI.pdf", "Rui_Sa_CV_SWE_FullStack.pdf", "Rui_Sa_CV_Enterprise_Tech.pdf"):
        (tmp_path / fname).write_bytes(b"%PDF-1.4 sample")
        (tmp_path / fname).with_suffix(".md").write_text("# Alex Silva\nPython, PyTorch, TypeScript", encoding="utf-8")
    return {
        "first_name": "Alex",
        "last_name": "Silva",
        "full_name": "Alex Silva",
        "email": "alex.silva@example.com",
        "phone": "+351 910 000 000",
        "location": "Setúbal, Portugal",
        "country": "Portugal",
        "linkedin_url": "https://www.linkedin.com/in/alex-silva/",
        "github_url": "https://github.com/example-user",
        "university": "Polytechnic Institute of Setúbal (IPS)",
        "degree": "BSc in Computer Engineering",
        "graduation_year": "2027",
        "cvs": {
            "ai_ml": str(tmp_path / "Rui_Sa_CV_Applied_AI.pdf"),
            "swe": str(tmp_path / "Rui_Sa_CV_SWE_FullStack.pdf"),
            "enterprise": str(tmp_path / "Rui_Sa_CV_Enterprise_Tech.pdf"),
        },
    }


def _navigator_job() -> EnrichedJob:
    return EnrichedJob(
        fingerprint="fp-nav-1",
        title="BEaPRO | Estágios Profissionais - People Analytics",
        company="The Navigator Company",
        location="Setúbal, Portugal",
        job_url="https://pt.linkedin.com/jobs/view/beapro-4491721283",
        canonical_url="https://pt.linkedin.com/jobs/view/beapro-4491721283",
        sources=["LinkedIn"],
        primary_source="LinkedIn",
        source_type="linkedin",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.DATA,
        geo_zone=GeoZone.SETUBAL_LOCAL,
        published_at=NOW,
        published_date="2026-11-27",
        freshness_label="Ontem",
        description_snippet="Estágio em People Analytics com Python, SQL e Power BI em Setúbal.",
        company_profile=CompanyProfile(
            company_name="The Navigator Company",
            normalized_name="the navigator company",
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=90.0,
        score_breakdown=ScoreBreakdown(total=90.0),
    )


def test_parse_natural_role_phrase_cleans_real_portal_titles():
    nav = parse_natural_role_phrase("BEaPRO | Estágios Profissionais - People Analytics", "The Navigator Company", pt=True)
    assert "|" not in nav["role_label"]
    assert "programa BEaPRO" in nav["apply_phrase"]
    assert "People Analytics" in nav["apply_phrase"]

    grant = parse_natural_role_phrase(
        "Public notice for one Research Grant - BI|2026/975 Projet SHELL– refª LISBOA 2030",
        "INESC ID",
        pt=True,
    )
    assert "SHELL" in grant["role_label"]
    assert "BI|2026/975" in grant["role_label"]
    assert "bolsa de investigação" in grant["apply_phrase"]

    nat = parse_natural_role_phrase("IT Intern | Natixis Trainee Program 2027 S1", "Natixis in Portugal", pt=False)
    assert "|" not in nat["role_label"]
    assert "Natixis Trainee Program" in nat["role_label"]

    bosch = parse_natural_role_phrase(
        "Extracurricular Internship | Functional Consulting – AI Sales Solutions (f/m/div.)",
        "Bosch",
        pt=False,
    )
    assert "(f/m/div.)" not in bosch["role_label"]
    assert "|" not in bosch["role_label"]
    assert "AI Sales Solutions" in bosch["role_label"]


def test_offline_kit_navigator_is_natural_pt(tmp_path):
    profile = _sample_profile(tmp_path)
    job = _navigator_job()
    kit = generate_application_kit(job, profile, fetch_email=False)
    assert kit["language"] == "pt"
    assert "à vaga 'BEaPRO" not in kit["short_cover_note"]
    assert "Estágios Profissionais -" not in kit["short_cover_note"]
    assert "ao programa BEaPRO" in kit["short_cover_note"]
    assert len(kit["role_tldr"]) >= 1


def test_gemini_structured_copy_and_graceful_fallback(tmp_path, monkeypatch):
    profile = _sample_profile(tmp_path)
    job = _navigator_job()
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")  # Legacy name must auto-upgrade!

    captured = {}

    class FakeInteractions:
        def create(self, **kwargs):
            captured.update(kwargs)
            payload = {
                "natural_role_title": "Programa BEaPRO — People Analytics",
                "role_tldr": ["Python e SQL para análise de dados de RH", "Modelos preditivos e dashboards Power BI", "Estágio em Setúbal"],
                "short_cover_note": "Olá equipa da The Navigator Company, candidato-me ao programa BEaPRO em People Analytics por juntar Python e SQL aplicados a dados reais em Setúbal.",
                "email_subject": "Candidatura — Programa BEaPRO People Analytics — Alex Silva",
                "email_body": "Exmos. Senhores,\n\nSegue o meu CV em anexo.",
                "recruiter_dm": "Olá! Candidatei-me ao programa BEaPRO em People Analytics na Navigator.",
                "highlight_bullets": ["Python + SQL", "IPS 17/20", "Deloitte 4,5 anos"],
            }
            return SimpleNamespace(output_text=json.dumps(payload))

    fake_client = SimpleNamespace(interactions=FakeInteractions())
    copy_res, model_used = generate_copy_with_gemini(
        job=job,
        description="Requisitos: Python, SQL, Power BI.",
        cv_markdown="Python, PyTorch",
        profile=profile,
        language="pt",
        client=fake_client,
    )
    assert copy_res is not None
    assert model_used == "gemini-4-hydrogen"
    assert captured["store"] is False
    assert captured["response_format"]["mime_type"] == "application/json"
    assert "Programa BEaPRO" in copy_res.natural_role_title

    class BrokenInteractions:
        def create(self, **kwargs):
            raise RuntimeError("quota exceeded")

    bad_res, msg = generate_copy_with_gemini(
        job=job,
        description="",
        cv_markdown="",
        profile=profile,
        language="pt",
        client=SimpleNamespace(interactions=BrokenInteractions()),
    )
    assert bad_res is None
    assert "quota exceeded" in msg


def test_detail_parsers_linkedin_greenhouse_smartrecruiters():
    li_html = """
    <html><body>
      <div class="show-more-less-html__markup">
        <p>Join our team in Setúbal using Python, SQL, and Power BI.</p>
      </div>
      <code id="applyUrl"><!--"https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fcareers.navigator.pt%2Fbeapro&urlhash=xyz"--></code>
    </body></html>
    """
    desc, ext = parse_linkedin_detail_html(li_html)
    assert "Python, SQL, and Power BI" in desc
    assert ext == "https://careers.navigator.pt/beapro"

    gh_desc = parse_greenhouse_detail_json({"content": "&lt;p&gt;Build scalable APIs in Rust &amp; Python.&lt;/p&gt;"})
    assert "Build scalable APIs" in gh_desc

    sr_desc = parse_smartrecruiters_detail_json({
        "defaultJobAd": {
            "sections": {
                "jobDescription": {"title": "Role", "text": "<p>AI Sales Solutions intern in Lisbon.</p>"},
                "qualifications": {"title": "Requirements", "text": "<p>Python and LLMs.</p>"},
            }
        }
    })
    assert "AI Sales Solutions intern" in sr_desc
    assert "Python and LLMs" in sr_desc


def test_dashboard_cors_cv_file_and_extension_endpoints(tmp_path):
    profile = _sample_profile(tmp_path)
    prof_path = tmp_path / "candidate_profile.yaml"
    prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")

    db = RadarStorage(tmp_path / "radar.db")
    db.upsert_jobs([_navigator_job()], now=NOW)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, profile_path=prof_path))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # OPTIONS CORS preflight
        opt_req = urllib.request.Request(f"{base}/api/profile", method="OPTIONS")
        with urllib.request.urlopen(opt_req, timeout=5) as opt_res:
            assert opt_res.status == 204
            assert opt_res.headers.get("Access-Control-Allow-Origin") == "*"

        # GET /api/cv-file
        with urllib.request.urlopen(f"{base}/api/cv-file?track=ai_ml", timeout=5) as pdf_res:
            assert pdf_res.headers.get("Content-Type") == "application/pdf"
            assert pdf_res.read().startswith(b"%PDF")

        # POST /api/answer-questions
        q_req = urllib.request.Request(
            f"{base}/api/answer-questions",
            data=json.dumps({
                "company": "Cloudflare",
                "title": "Software Engineer Intern",
                "page_text": "Internship in Lisbon",
                "questions": ["Do you require visa sponsorship?", "Why Cloudflare?"],
                "cv_track": "swe",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ans = json.loads(urllib.request.urlopen(q_req, timeout=5).read().decode("utf-8"))
        assert ans["ok"] is True
        assert len(ans["answers"]) == 2

        # POST /api/generate-for-page
        pg_req = urllib.request.Request(
            f"{base}/api/generate-for-page",
            data=json.dumps({
                "url": "https://boards.greenhouse.io/cloudflare/jobs/99",
                "title": "Software Engineer Intern",
                "page_text": "Build distributed systems in Lisbon using Rust, Go and Python.",
                "cv_track": "swe",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        pg_kit = json.loads(urllib.request.urlopen(pg_req, timeout=5).read().decode("utf-8"))
        assert pg_kit["company"] == "Cloudflare"
        assert "Cloudflare" in pg_kit["short_cover_note"]
    finally:
        server.shutdown()
        server.server_close()


def test_playwright_v2_cookies_selects_hidden_file_and_open_question(tmp_path):
    pytest.importorskip("playwright")
    profile = _sample_profile(tmp_path)
    job = _navigator_job()
    kit = generate_application_kit(job, profile, fetch_email=False)

    mock_html = tmp_path / "mock_modern_ats.html"
    mock_html.write_text(
        """<!doctype html>
        <html><body>
          <div id="cookie-banner">
            <button id="onetrust-accept-btn-handler" onclick="document.getElementById('cookie-banner').style.display='none'">Accept All Cookies</button>
          </div>
          <form id="modern-form">
            <label for="full_name">Full Name</label>
            <input id="full_name" name="name" type="text">
            <label for="email">Email Address</label>
            <input id="email" name="email" type="email">
            <label for="country">Country</label>
            <select id="country" name="country">
              <option value="">--</option>
              <option value="PT">Portugal</option>
              <option value="US">United States</option>
            </select>
            <label for="sponsor">Will you require visa sponsorship?</label>
            <select id="sponsor" name="sponsorship">
              <option value="">--</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
            <label for="why_q">Why do you want to join our team?</label>
            <textarea id="why_q" name="custom_question_1"></textarea>
            <label for="resume_file">Upload CV</label>
            <input id="resume_file" name="resume" type="file" style="display:none">
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
        user_data_dir=tmp_path / "chrome_prof",
    )
    assert res["ok"] is True
    assert res["cv_attached"] is True
    assert {"full_name", "email", "country", "sponsorship", "cv_pdf"}.issubset(set(res["filled_fields"]))
