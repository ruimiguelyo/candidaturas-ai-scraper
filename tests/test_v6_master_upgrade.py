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
    profile["preferred_locations"] = ["Lisbon", "Portugal - Remote", "Portugal"]
    profile["pronouns"] = "He/him"
    mock_ats.write_text(
        """<!doctype html>
        <html><body>
          <header><input id="hidden-search" type="search" placeholder="Site search" style="display:none"></header>
          <form id="rev-form">
            <div>CV / Resume
              <input id="cv_in" type="file" name="file" aria-label="Click to attach document" style="display:none"
                onchange="setTimeout(() => {
                  document.getElementById('fn').value='Alex Silva';
                  document.getElementById('em').value='alex.silva@example.com';
                  document.getElementById('ph').value='+351910000000';
                  document.getElementById('li').value='';
                  document.getElementById('gh').value='';
                }, 220);">
            </div>
            <input id="fn" type="text" placeholder="Full name">
            <input id="em" type="text" placeholder="Email">
            <div>
              <span><span><div><label><input id="code-btn" type="button" aria-label="Search phone country codes" value="+44" onclick="openPortal('phone')"></label></div></span></span>
              <input id="ph" type="tel" name="phoneNumber" placeholder="Phone number">
            </div>
            <div>
              <label><span>Current country</span><span><div><input id="country-btn" type="button" value="United Kingdom" onclick="openPortal('country')"></div></span></label>
              <label><span>Preferred work locations</span><span><div><input id="loc-btn" type="button" value="" onclick="openPortal('loc')"></div></span></label>
            </div>
            <div>
              <p>Select gender you identify with (optional)</p>
              <label><span><span><div><input id="gender-btn" type="button" value="Select gender you identify with (optional)" onclick="openPortal('gender')"></div></span></span></label>
            </div>
            <div>
              <p>1. How did you hear about our job posting? (optional)</p>
              <label><span><span><div><input id="hear-btn" type="button" value="Select one" onclick="openPortal('hear')"></div></span></span></label>
              <p>2. Have we met at a conference or event? Tell us which one. (optional)</p>
              <label><span><span><div><input id="conf-btn" type="button" value="Select one" onclick="openPortal('conf')"></div></span></span></label>
            </div>
            <div>
              <p>1. Have you previously been employed by Revolut?</p>
              <label><span><span><div><input id="emp-btn" type="button" value="Select one" onclick="openPortal('emp')"></div></span></span></label>
            </div>
            <div>
              <p>1. Select the university you've graduated (or are about to graduate) from If not listed, select “Other – not listed"</p>
              <label><span><span><div><input id="uni-btn" type="button" value="Select one" onclick="openPortal('uni')"></div></span></span></label>
              <p>2. Select your graduation year</p>
              <label><span><span><div><input id="grad-btn" type="button" value="Select one" onclick="openPortal('grad')"></div></span></span></label>
              <p>3. Select your faculty</p>
              <label><span><span><div><input id="fac-btn" type="button" value="Select one" onclick="openPortal('fac')"></div></span></span></label>
              <p>4. Have you participated in any Academic Olympiads or other internationally recognised competitions?</p>
              <label><span><span><div><input id="oly-btn" type="button" value="Select one" onclick="openPortal('oly')"></div></span></span></label>
            </div>
            <div>
              <p>1. Are you available for the full duration of the programme?</p>
              <label><span><span><div><input id="avail-btn" type="button" value="Select one" onclick="openPortal('avail')"></div></span></span></label>
              <p>2. What is your preferred start date?</p>
              <label><span><span><div><input id="start-btn" type="button" value="Select one" onclick="openPortal('start')"></div></span></span></label>
            </div>
            <div>
              <p>1. What are your pronouns?</p>
              <div>
                <label><input id="pron-he" type="checkbox"> He/him</label>
                <label><input id="pron-she" type="checkbox"> She/her</label>
              </div>
            </div>
            <div>
              <p>Consent to send transcripts</p>
              <label><input id="consent-yes" type="radio" name="consent"> Yes, I consent</label>
            </div>
            <input id="li" type="text" placeholder="Link to your LinkedIn profile (optional)">
            <input id="gh" type="text" placeholder="Links to your Github, portfolio, etc. (optional)">
          </form>
          <div id="portal-root"></div>
          <script>
            window.__SENSITIVE_TOUCHED = false;
            window.__LOC_SELECTED = [];
            let activeKind = null;
            function closePortal() {
              document.getElementById('portal-root').innerHTML = '';
              activeKind = null;
            }
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePortal(); });
            function renderPortal(kind, filterText) {
              activeKind = kind;
              const q = (filterText || '').trim().toLowerCase();
              const root = document.getElementById('portal-root');
              let searchHtml = '';
              let items = [];
              let multiselect = false;
              if (kind === 'phone') {
                searchHtml = '<input id="portal-search" type="search" aria-label="Search" value="' + (filterText || '') + '">';
                items = q.includes('portug') ? ['Portugal (+351)'] : ['+44 United Kingdom', '+353 Ireland'];
              } else if (kind === 'country') {
                searchHtml = '<input id="portal-search" type="search" aria-label="Search" value="' + (filterText || '') + '">';
                items = q.includes('portug') ? ['Portugal'] : ['Afghanistan', 'Albania', 'United Kingdom'];
              } else if (kind === 'loc') {
                multiselect = true;
                items = ['Barcelona', 'Poland - Remote', 'Lisbon', 'Portugal - Remote', 'Spain - Remote', 'UK - Remote'];
              } else if (kind === 'hear') {
                items = ['Friends/family', 'Job portal or job search (Indeed, LinkedIn, Glassdoor, etc.)'];
              } else if (kind === 'emp') {
                items = ['Yes', 'Yes, through an external vendor or contract', 'No'];
              } else if (kind === 'uni') {
                searchHtml = '<input id="portal-search" type="search" aria-label="Search" value="' + (filterText || '') + '">';
                items = q.includes('other') ? ['Other – not listed'] : ['Oxford University', 'Cambridge University'];
              } else if (kind === 'grad') {
                items = ['2026', '2027', '2028'];
              } else if (kind === 'fac') {
                items = ['Arts', 'Computer Science', 'Economics'];
              } else if (kind === 'avail') {
                items = ['Yes', 'No'];
              } else if (kind === 'start') {
                items = ['September 2027', 'January 2028'];
              } else {
                items = ['Sensitive Opt'];
              }
              const listHtml = items.map(txt => {
                if (multiselect) {
                  const chk = window.__LOC_SELECTED.includes(txt) ? 'checked' : '';
                  return '<div role="option"><input type="checkbox" ' + chk + '> ' + txt + '</div>';
                }
                return '<button type="button" role="option">' + txt + '</button>';
              }).join('');
              root.innerHTML = '<div data-rui="dropdown">' + searchHtml + '<div role="listbox"' + (multiselect ? ' aria-multiselectable="true"' : '') + '>' + listHtml + '</div></div>';
              const ps = document.getElementById('portal-search');
              if (ps) {
                ps.addEventListener('input', (ev) => renderPortal(kind, ev.target.value));
              }
              root.querySelectorAll('[role="option"]').forEach(opt => {
                opt.addEventListener('click', () => {
                  const t = opt.innerText.trim();
                  if (kind === 'loc') {
                    const idx = window.__LOC_SELECTED.indexOf(t);
                    if (idx >= 0) window.__LOC_SELECTED.splice(idx, 1);
                    else window.__LOC_SELECTED.push(t);
                    document.getElementById('loc-btn').value = window.__LOC_SELECTED.join(', ');
                    renderPortal('loc', '');
                    return;
                  }
                  const map = {phone:'code-btn', country:'country-btn', hear:'hear-btn', emp:'emp-btn', uni:'uni-btn', grad:'grad-btn', fac:'fac-btn', avail:'avail-btn', start:'start-btn'};
                  if (map[kind]) {
                    document.getElementById(map[kind]).value = (kind === 'phone' ? '+351' : t);
                  } else {
                    window.__SENSITIVE_TOUCHED = true;
                  }
                  closePortal();
                });
              });
            }
            window.openPortal = (kind) => renderPortal(kind, '');
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
    assert {
        "cv_pdf",
        "phone_country",
        "country",
        "preferred_location",
        "source",
        "prev_employed",
        "university",
        "graduation_year",
        "faculty",
        "availability",
        "start_date",
        "pronouns",
        "phone",
        "linkedin_url",
        "github_url",
    }.issubset(filled)
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
    profile["last_name"] = "Sá"
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
    assert pdf_file.name == "Alex_Sa_CV.pdf"
    assert pdf_file.parent.name == f"Revolut_{job.fingerprint[:6]}"
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
