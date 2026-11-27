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

from estagio_radar.ai_writer import (
    answer_form_questions_with_gemini,
    generate_copy_with_gemini,
    parse_natural_role_phrase,
    verify_gemini_api_key,
)
from estagio_radar.apply_copilot import generate_application_kit
from estagio_radar.autofill_worker import (
    _launch_persistent,
    _run_linkedin_easy_apply_assistant,
    read_autofill_status,
    run_autofill_browser,
    write_autofill_status,
)
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


def test_form_answers_list_schema_and_gemini_key_verification(tmp_path, monkeypatch):
    profile = _sample_profile(tmp_path)

    class OkClient:
        class interactions:
            @staticmethod
            def create(**kwargs):
                if "schema" in (kwargs.get("response_format") or {}):
                    return SimpleNamespace(
                        output_text=json.dumps({
                            "answers": [
                                {"question": "Why Cloudflare?", "answer": "I build fast distributed TypeScript and Python systems."},
                            ]
                        })
                    )
                return SimpleNamespace(output_text="OK")

    ok, used_model, msg = verify_gemini_api_key("AQ.FakeTestKey123", client=OkClient())
    assert ok is True
    assert used_model == "gemini-4-hydrogen"
    assert "validado" in msg

    ans = answer_form_questions_with_gemini(
        questions=["Why Cloudflare?"],
        company="Cloudflare",
        title="SWE Intern",
        page_text="Lisbon",
        cv_markdown="Python, TypeScript",
        profile=profile,
        client=OkClient(),
    )
    assert ans["Why Cloudflare?"] == "I build fast distributed TypeScript and Python systems."

    class BadClient:
        class interactions:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("API_KEY_INVALID")

    ok_bad, _m, bad_msg = verify_gemini_api_key("AQ.Invalid", client=BadClient())
    assert ok_bad is False
    assert "API_KEY_INVALID" in bad_msg

    # Testar /api/settings/gemini-key (200 valido / 400 invalido) + /api/autofill-status sem sujar .env real
    env_calls = []
    monkeypatch.setattr("estagio_radar.dashboard._write_env_setting", lambda root, k, v: env_calls.append((k, v)))
    monkeypatch.setattr(
        "estagio_radar.dashboard.verify_gemini_api_key",
        lambda key: (True, "gemini-4-hydrogen", "OK!") if key.startswith("AQ.Valid") else (False, "gemini-4-hydrogen", "Bad key"),
    )

    prof_path = tmp_path / "candidate_profile.yaml"
    prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
    db = RadarStorage(tmp_path / "radar.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, profile_path=prof_path))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        bad_req = urllib.request.Request(
            f"{base}/api/settings/gemini-key",
            data=json.dumps({"api_key": "bad-key"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(bad_req, timeout=5)
        assert exc_info.value.code == 400
        assert env_calls == []

        ok_req = urllib.request.Request(
            f"{base}/api/settings/gemini-key",
            data=json.dumps({"api_key": "AQ.ValidKey123"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ok_resp = json.loads(urllib.request.urlopen(ok_req, timeout=5).read().decode("utf-8"))
        assert ok_resp["ok"] is True
        assert env_calls == [("GEMINI_API_KEY", "AQ.ValidKey123")]

        st = json.loads(urllib.request.urlopen(f"{base}/api/autofill-status", timeout=5).read().decode("utf-8"))
        assert "status" in st
    finally:
        server.shutdown()
        server.server_close()

    status_file = tmp_path / "last_autofill.json"
    write_autofill_status({"status": "done", "message": "Teste OK"}, status_file)
    assert read_autofill_status(status_file)["status"] == "done"


def test_playwright_v3_combobox_radios_easy_apply_stops_before_submit(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    profile = _sample_profile(tmp_path)
    job = _navigator_job()
    kit = generate_application_kit(job, profile, fetch_email=False)

    mock_easy = tmp_path / "mock_easy_apply.html"
    mock_easy.write_text(
        """<!doctype html>
        <html><body>
          <script>window.__SUBMIT_CLICKED = false;</script>
          <button class="jobs-apply-button" onclick="document.getElementById('modal').style.display='block'">Easy Apply</button>
          <div id="modal" role="dialog" style="display:none">
            <div id="step-1">
              <label for="phone_inp">Phone</label>
              <input id="phone_inp" name="phone" type="tel">
              <label for="country_cb">Country</label>
              <input id="country_cb" role="combobox" type="text">
              <div role="listbox">
                <div role="option" onclick="document.getElementById('country_cb').value='Portugal'">Portugal</div>
                <div role="option">Spain</div>
              </div>
              <fieldset>
                <legend>Are you legally authorized to work in the EU / Portugal?</legend>
                <label><input type="radio" name="work_auth" value="yes"> Yes</label>
                <label><input type="radio" name="work_auth" value="no"> No</label>
              </fieldset>
              <button type="button" onclick="document.getElementById('step-1').style.display='none';document.getElementById('step-2').style.display='block'">Next</button>
            </div>
            <div id="step-2" style="display:none">
              <label for="cv_up">Upload Resume</label>
              <input id="cv_up" name="resume" type="file" style="display:none">
              <button type="button" onclick="document.getElementById('step-2').style.display='none';document.getElementById('step-3').style.display='block'">Review</button>
            </div>
            <div id="step-3" style="display:none">
              <button type="submit" id="final-submit" onclick="window.__SUBMIT_CLICKED = true">Submit application</button>
            </div>
          </div>
        </body></html>""",
        encoding="utf-8",
    )

    res = run_autofill_browser(
        job,
        kit,
        profile,
        headless=True,
        keep_open=False,
        target_url=mock_easy.as_uri(),
        user_data_dir=tmp_path / "chrome_easy",
    )
    assert res["ok"] is True
    assert res["stopped_before_submit"] is True
    assert res["cv_attached"] is True
    assert res["steps_completed"] == 3
    assert {"phone", "country", "work_auth", "cv_pdf"}.issubset(set(res["filled_fields"]))

    # Confirmar separadamente numa pagina CAPTCHA que a automacao para sem tocar em nada
    mock_captcha = tmp_path / "mock_captcha.html"
    mock_captcha.write_text(
        """<!doctype html>
        <html><body>
          <div class="cf-turnstile">Verify you are human</div>
          <iframe src="https://challenges.cloudflare.com/turnstile/v0/widget" width="300" height="65"></iframe>
          <form><input name="email" type="email"></form>
        </body></html>""",
        encoding="utf-8",
    )
    res_cap = run_autofill_browser(
        job,
        kit,
        profile,
        headless=True,
        keep_open=False,
        target_url=mock_captcha.as_uri(),
        user_data_dir=tmp_path / "chrome_cap",
    )
    assert res_cap["captcha_detected"] is True
    assert res_cap["status"] == "captcha_blocked"
    assert res_cap["filled_fields"] == []

    # Garantir diretamente no browser real que o assistente preenche os passos e NUNCA clica Submit
    with sync_playwright() as p:
        ctx = _launch_persistent(p, tmp_path / "chrome_direct", headless=True)
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(mock_easy.as_uri(), wait_until="domcontentloaded")
            _run_linkedin_easy_apply_assistant(
                pg,
                job=job,
                kit=kit,
                profile=profile,
                values={"phone": profile["phone"]},
                cv_path=Path(kit["cv"]["path"]),
            )
            assert pg.evaluate("window.__SUBMIT_CLICKED") is False
            assert pg.locator("#final-submit").is_visible() is True
        finally:
            ctx.close()
