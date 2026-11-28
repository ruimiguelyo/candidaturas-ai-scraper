"""Testes completos do v6.0: Autofill v4 (reset CV parser + dropdowns), Follow-up CRM e CV PDF adaptado."""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from estagio_radar.apply_copilot import generate_application_kit
from estagio_radar.autofill_worker import run_autofill_browser
from estagio_radar.cv_tailor import (
    TailoredCvPatch,
    generate_tailored_cv_pdf,
    tailor_cv_html,
)
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

NOW = datetime(2026, 11, 28, 1, 0, tzinfo=timezone.utc)

BASE_SAMPLE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><style>@page { size: A4; margin: 10mm; }</style></head>
<body>
  <header class="header">
    <div class="name">Alex Silva</div>
    <div class="headline">Junior Engineer | Python & TypeScript</div>
  </header>
  <section class="section">
    <h2 class="section-title">Summary</h2>
    <p class="summary">BSc Computer Engineering student at IPS building AI and full-stack software.</p>
  </section>
  <section class="section">
    <h2 class="section-title">Projects</h2>
    <div class="entry"><span class="entry-title">Project Alpha</span><ul><li>Built API</li></ul></div>
    <div class="entry"><span class="entry-title">Project Beta AI</span><ul><li>Trained PyTorch LLM eval</li></ul></div>
    <div class="entry"><span class="entry-title">Project Gamma</span><ul><li>CI/CD pipeline</li></ul></div>
  </section>
  <section class="section">
    <h2 class="section-title">Education</h2>
    <div>IPS BSc Computer Engineering</div>
  </section>
</body>
</html>
"""


def _make_sample_job() -> EnrichedJob:
    return EnrichedJob(
        fingerprint="fp-v6-rev",
        title="Graduate Programme 2027: Applied AI Engineer",
        company="Revolut",
        location="Lisbon / Remote",
        job_url="https://www.linkedin.com/jobs/view/revolut-4478449395",
        canonical_url="https://www.linkedin.com/jobs/view/revolut-4478449395",
        sources=["LinkedIn"],
        primary_source="LinkedIn",
        source_type="linkedin",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.REMOTE_PT,
        description_snippet="Build PyTorch & LLM systems in Python with SQL pipelines.",
        company_profile=CompanyProfile(
            company_name="Revolut",
            normalized_name="revolut",
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=90.0,
        score_breakdown=ScoreBreakdown(total=90.0),
    )


def _sample_profile(tmp_path: Path) -> dict:
    for fname in ("Alex_CV_Applied_AI.pdf", "Alex_CV_SWE_FullStack.pdf", "Alex_CV_Enterprise_Tech.pdf"):
        pdf_p = tmp_path / fname
        pdf_p.write_bytes(b"%PDF-1.4 sample")
        pdf_p.with_suffix(".md").write_text("# Alex Silva\nPython, PyTorch, LLM Evaluation, SQL", encoding="utf-8")
        pdf_p.with_suffix(".html").write_text(BASE_SAMPLE_HTML, encoding="utf-8")
    return {
        "first_name": "Alex",
        "last_name": "Silva",
        "full_name": "Alex Silva",
        "email": "alex.silva@example.com",
        "phone": "+351 910 000 000",
        "location": "Setúbal, Portugal",
        "country": "Portugal",
        "preferred_locations": ["Lisbon", "Portugal", "Remote"],
        "linkedin_url": "https://www.linkedin.com/in/alex-silva/",
        "github_url": "https://github.com/example-user",
        "university": "Polytechnic Institute of Setúbal (IPS)",
        "degree": "BSc in Computer Engineering",
        "graduation_year": "2027",
        "cvs": {
            "ai_ml": str(tmp_path / "Alex_CV_Applied_AI.pdf"),
            "swe": str(tmp_path / "Alex_CV_SWE_FullStack.pdf"),
            "enterprise": str(tmp_path / "Alex_CV_Enterprise_Tech.pdf"),
        },
    }


def test_playwright_v4_cv_parser_reset_immunity_and_ancestor_dropdowns(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _sample_profile(tmp_path)
    job = _make_sample_job()
    kit = generate_application_kit(job, profile, fetch_email=False)

    mock_ats = tmp_path / "mock_revolut_async.html"
    mock_ats.write_text(
        """<!doctype html>
        <html><body>
          <form id="rev-form">
            <div>CV / Resume
              <input id="cv_in" type="file" name="file" aria-label="Click to attach document" style="display:none"
                onchange="setTimeout(() => {
                  document.getElementById('fn').value='Alex Silva';
                  document.getElementById('em').value='alex.silva@example.com';
                  document.getElementById('ph').value='';
                  document.getElementById('li').value='';
                  document.getElementById('gh').value='';
                }, 220);">
            </div>
            <input id="fn" type="text" placeholder="Full name">
            <input id="em" type="text" placeholder="Email">
            <div>
              <input type="button" aria-label="Search phone country codes" value="+44" onclick="openMenu('code-menu')">
              <div id="code-menu" role="listbox" style="display:none">
                <button type="button" role="option" onclick="selectOpt('code-menu', this)">Portugal (+351)</button>
                <button type="button" role="option">United Kingdom (+44)</button>
              </div>
              <input id="ph" type="tel" name="phoneNumber" placeholder="Phone number">
            </div>
            <div>
              <p>1. How did you hear about our job posting? (optional)</p>
              <div>
                <button type="button" aria-haspopup="listbox" onclick="openMenu('hear-menu')">Select one</button>
              </div>
              <div id="hear-menu" role="listbox" style="display:none">
                <div role="option" onclick="selectPrevBtn(this, 'hear-menu')">LinkedIn</div>
                <div role="option">Other</div>
              </div>
            </div>
            <div>
              <p>1. Have you previously been employed by Revolut?</p>
              <div>
                <button type="button" aria-haspopup="listbox" onclick="openMenu('emp-menu')">Select one</button>
              </div>
              <div id="emp-menu" role="listbox" style="display:none">
                <div role="option" onclick="selectPrevBtn(this, 'emp-menu')">Yes</div>
                <div role="option" onclick="selectPrevBtn(this, 'emp-menu')">No</div>
              </div>
            </div>
            <div>
              <p>Select ethnicity (optional)</p>
              <div>
                <button id="eth-btn" type="button" aria-haspopup="listbox" onclick="openMenu('eth-menu')">Select ethnicity (optional)</button>
              </div>
              <div id="eth-menu" role="listbox" style="display:none">
                <div role="option" onclick="window.__ETH_TOUCHED=true">Prefer not to say</div>
              </div>
            </div>
            <input id="li" type="text" placeholder="Link to your LinkedIn profile (optional)">
            <input id="gh" type="text" placeholder="Links to your Github, portfolio, etc. (optional)">
          </form>
          <script>
            window.__ETH_TOUCHED = false;
            function openMenu(id) {
              document.querySelectorAll("[role='listbox']").forEach(m => m.style.display='none');
              document.getElementById(id).style.display='block';
            }
            function selectOpt(menuId, el) {
              document.getElementById(menuId).style.display='none';
              el.closest('div').parentElement.querySelector("input[type='button']").value = el.innerText;
            }
            function selectPrevBtn(optEl, menuId) {
              const wrap = document.getElementById(menuId).parentElement;
              wrap.querySelector("button[aria-haspopup]").innerText = optEl.innerText;
              document.getElementById(menuId).style.display='none';
            }
          </script>
        </body></html>""",
        encoding="utf-8",
    )

    res = run_autofill_browser(
        job,
        kit,
        profile,
        headless=True,
        keep_open=False,
        target_url=mock_ats.as_uri(),
        user_data_dir=tmp_path / "chrome_v4",
    )
    assert res["status"] == "done"
    assert res["cv_attached"] is True
    filled = set(res["filled_fields"])
    assert {"cv_pdf", "phone_country", "source", "prev_employed", "phone", "linkedin_url", "github_url"}.issubset(filled)
    assert res.get("url", "").endswith("mock_revolut_async.html")


def test_crm_followup_lifecycle_and_dashboard_endpoints(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _sample_profile(tmp_path)
    prof_path = tmp_path / "candidate_profile.yaml"
    prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")

    db = RadarStorage(tmp_path / "radar.db")
    job = _make_sample_job()
    db.upsert_jobs([job], now=NOW)

    # Marcar como candidatado há 6 dias -> followup já vencido!
    six_days_ago = NOW - timedelta(days=6)
    assert db.update_job_status(job.fingerprint, "applied", "Enviado", now=six_days_ago) is True
    due = db.list_due_followups(now=NOW)
    assert len(due) == 1
    assert due[0].fingerprint == job.fingerprint
    assert due[0].followup_at is not None

    kit = generate_application_kit(due[0], profile, fetch_email=False)
    assert "hiring_manager_search_url" in kit
    assert "Revolut" in kit["followup_dm"]
    assert len(kit["followup_dm"]) <= 280

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, profile_path=prof_path))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        res_due = json.loads(urllib.request.urlopen(f"{base}/api/followups", timeout=5).read().decode("utf-8"))
        assert res_due["count"] == 1

        req_done = urllib.request.Request(
            f"{base}/api/followup-done",
            data=json.dumps({"fingerprint": job.fingerprint, "snooze_days": 7}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        res_done = json.loads(urllib.request.urlopen(req_done, timeout=5).read().decode("utf-8"))
        assert res_done["ok"] is True

        res_after = json.loads(urllib.request.urlopen(f"{base}/api/followups", timeout=5).read().decode("utf-8"))
        assert res_after["count"] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_tailor_cv_html_and_one_page_pdf_api_integration(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    profile = _sample_profile(tmp_path)
    prof_path = tmp_path / "candidate_profile.yaml"
    prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")

    job = _make_sample_job()
    patch = TailoredCvPatch(
        headline="Applied AI Engineer Graduate | PyTorch, LLM Evaluation & Python",
        summary_plain="Final-year CS student building typed LLM evaluation and PyTorch workflows.",
        project_order=[1, 0, 2],
        matched_keywords=["python", "pytorch", "llm"],
    )
    html_out = tailor_cv_html(BASE_SAMPLE_HTML, patch)
    assert "Applied AI Engineer Graduate" in html_out
    assert html_out.index("Project Beta AI") < html_out.index("Project Alpha")
    assert "IPS BSc Computer Engineering" in html_out

    class MockGeminiClient:
        class interactions:
            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(output_text=patch.model_dump_json())

    monkeypatch.setenv("GEMINI_API_KEY", "AQ.FakeTestKey123")
    res_pdf = generate_tailored_cv_pdf(
        job,
        base_cv_pdf=Path(profile["cvs"]["ai_ml"]),
        description="Looking for Applied AI Engineer with PyTorch and LLM experience.",
        profile=profile,
        out_dir=tmp_path / "tailored",
        client=MockGeminiClient(),
    )
    pdf_file = Path(res_pdf["path"])
    assert pdf_file.exists()
    assert pdf_file.read_bytes().startswith(b"%PDF")

    db = RadarStorage(tmp_path / "radar.db")
    db.upsert_jobs([job], now=NOW)
    monkeypatch.setattr(
        "estagio_radar.dashboard.generate_tailored_cv_pdf",
        lambda j, **kw: res_pdf,
    )
    spawned_cmds = []

    class DummyPopen:
        def __init__(self, cmd, **kwargs):
            spawned_cmds.append(cmd)

    monkeypatch.setattr("estagio_radar.dashboard.subprocess.Popen", DummyPopen)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, profile_path=prof_path))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        req_tailor = urllib.request.Request(
            f"{base}/api/tailor-cv",
            data=json.dumps({"fingerprint": job.fingerprint, "cv_track": "ai_ml"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t_resp = json.loads(urllib.request.urlopen(req_tailor, timeout=5).read().decode("utf-8"))
        assert t_resp["ok"] is True
        assert t_resp["cv"]["tailored"] is True

        # Servir PDF adaptado via /api/cv-file?fingerprint=...
        pdf_dl = urllib.request.urlopen(f"{base}/api/cv-file?fingerprint={job.fingerprint}", timeout=5).read()
        assert pdf_dl.startswith(b"%PDF")

        # Autofill passa a escolher automaticamente o CV adaptado!
        req_auto = urllib.request.Request(
            f"{base}/api/autofill",
            data=json.dumps({"fingerprint": job.fingerprint, "use_tailored": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        a_resp = json.loads(urllib.request.urlopen(req_auto, timeout=5).read().decode("utf-8"))
        assert a_resp["cv"]["filename"] == pdf_file.name
    finally:
        server.shutdown()
        server.server_close()
