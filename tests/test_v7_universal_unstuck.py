"""Testes do Universal Unstuck Engine v7.0: SPA Workday, Login/Guest, AI DOM fallback, Wizard e HUD PNA CORS."""

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

from estagio_radar.ai_writer import pick_unstuck_action_with_gemini
from estagio_radar.apply_copilot import generate_application_kit
from estagio_radar.autofill_worker import (
    _fill_current_page,
    _follow_apply_hops,
    _launch_persistent,
    run_autofill_browser,
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

NOW = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)


def _sample_job(company: str = "Richemont", fp: str = "fp-v7-rich") -> EnrichedJob:
    return EnrichedJob(
        fingerprint=fp,
        title="Professional Internship - Tech",
        company=company,
        location="Moscavide / Lisbon",
        job_url="https://richemont.wd3.myworkdayjobs.com/pt-BR/richemont/job/MOSCAVIDE/Tech_JR134086/apply",
        canonical_url="https://richemont.wd3.myworkdayjobs.com/pt-BR/richemont/job/MOSCAVIDE/Tech_JR134086/apply",
        sources=["LinkedIn"],
        primary_source="LinkedIn",
        source_type="linkedin",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.SWE,
        geo_zone=GeoZone.LISBOA_METRO,
        company_profile=CompanyProfile(
            company_name=company,
            normalized_name=company.lower(),
            company_tier="A",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=86.0,
        score_breakdown=ScoreBreakdown(total=86.0),
    )


def _sample_profile(tmp_path: Path) -> dict:
    cv_pdf = tmp_path / "Alex_Sa_CV_SWE.pdf"
    cv_pdf.write_bytes(b"%PDF-1.4 sample")
    cv_pdf.with_suffix(".md").write_text("# Alex Sá\nPython, TypeScript, React", encoding="utf-8")
    return {
        "first_name": "Alex",
        "last_name": "Sá",
        "full_name": "Alex Sá",
        "email": "alex.sa@example.com",
        "phone": "+351 910 000 000",
        "city": "Setúbal",
        "country": "Portugal",
        "location": "Setúbal, Portugal",
        "linkedin_url": "https://www.linkedin.com/in/alex-sa/",
        "github_url": "https://github.com/example-user",
        "university": "Polytechnic Institute of Setúbal (IPS)",
        "degree": "BSc in Computer Engineering",
        "graduation_year": "2027",
        "cvs": {
            "ai_ml": str(cv_pdf),
            "swe": str(cv_pdf),
            "enterprise": str(cv_pdf),
        },
    }


def test_workday_spa_delayed_hydration_chooser_and_multistep_wizard(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _sample_profile(tmp_path)
    job = _sample_job()
    kit = generate_application_kit(job, profile, fetch_email=False)

    mock_wd = tmp_path / "mock_workday_spa.html"
    mock_wd.write_text(
        """<!doctype html>
        <html><body>
          <div id="app">
            <div data-automation-id="loadingSpinner" role="progressbar" style="width:40px;height:40px">Loading...</div>
          </div>
          <div id="wd-portal"></div>
          <script>
            window.__WRONG_CLICK = null;
            window.__SUBMIT_CLICKED = false;

            function renderChooser() {
              document.getElementById('app').innerHTML = `
                <header>
                  <button data-automation-id="utilityButtonSignIn" onclick="window.__WRONG_CLICK='signin'">Fazer login</button>
                </header>
                <main>
                  <h1>Comece sua candidatura</h1>
                  <a data-automation-id="applyManually" href="#step1" onclick="event.preventDefault(); renderStep1()">Candidatar-se manualmente</a>
                  <a data-automation-id="useMyLastApplication" href="#last" onclick="event.preventDefault(); window.__WRONG_CLICK='last'">Usar minha última candidatura</a>
                  <button onclick="window.__WRONG_CLICK='linkedin'">Candidate-se pelo LinkedIn</button>
                </main>
              `;
            }

            window.renderStep1 = function() {
              document.getElementById('app').innerHTML = `
                <main>
                  <h2>My Information</h2>
                  <button type="button" aria-haspopup="listbox" data-automation-id="countryDropdown" onclick="openWdList('country')">Select one</button>
                  <input id="wd-fn" type="text" data-automation-id="legalNameSection_firstName" placeholder="First Name">
                  <input id="wd-ln" type="text" data-automation-id="legalNameSection_lastName" placeholder="Last Name">
                  <input id="wd-city" type="text" data-automation-id="addressSection_city" placeholder="City">
                  <button type="button" aria-haspopup="listbox" data-automation-id="phone-device-type" onclick="openWdList('device')">Select one</button>
                  <input id="wd-ph" type="tel" data-automation-id="phone-number" placeholder="Phone Number">
                  <input id="wd-cv" type="file" data-automation-id="file-upload-input-ref" style="display:none">
                  <button type="button" data-automation-id="bottom-navigation-next-button" onclick="renderStep2()">Save and Continue</button>
                </main>
              `;
            };

            window.openWdList = function(kind) {
              const opts = kind === 'country'
                ? ['Spain', 'Portugal']
                : (kind === 'device' ? ['Home', 'Mobile'] : (kind === 'source' ? ['Company Site', 'LinkedIn'] : ['Yes', 'No']));
              document.getElementById('wd-portal').innerHTML =
                '<div role="listbox">' +
                opts.map(o => `<div role="option" data-automation-id="promptOption" onclick="pickWdOpt('${kind}', '${o}')">${o}</div>`).join('') +
                '</div>';
            };

            window.pickWdOpt = function(kind, val) {
              const selMap = {
                country: "[data-automation-id='countryDropdown']",
                device: "[data-automation-id='phone-device-type']",
                source: "[data-automation-id='sourceSection']",
                prev: "[data-automation-id='previousWorker']"
              };
              const btn = document.querySelector(selMap[kind]);
              if (btn) btn.innerText = val;
              document.getElementById('wd-portal').innerHTML = '';
            };

            window.renderStep2 = function() {
              document.getElementById('app').innerHTML = `
                <main>
                  <h2>Application Questions</h2>
                  <div>
                    <p>How did you hear about us?</p>
                    <button type="button" aria-haspopup="listbox" data-automation-id="sourceSection" onclick="openWdList('source')">Select one</button>
                  </div>
                  <div>
                    <p>Have you previously worked for Richemont?</p>
                    <button type="button" aria-haspopup="listbox" data-automation-id="previousWorker" onclick="openWdList('prev')">Select one</button>
                  </div>
                  <button type="button" data-automation-id="bottom-navigation-next-button" onclick="renderStep3()">Save and Continue</button>
                </main>
              `;
            };

            window.renderStep3 = function() {
              document.getElementById('app').innerHTML = `
                <main>
                  <h2>Review</h2>
                  <p>Everything ready to submit.</p>
                  <button type="button" onclick="window.__SUBMIT_CLICKED=true">Submit Application</button>
                </main>
              `;
            };

            setTimeout(renderChooser, 350);
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
        target_url=mock_wd.as_uri(),
        user_data_dir=tmp_path / "chrome_wd",
    )
    assert res["status"] == "done"
    assert res["cv_attached"] is True
    assert res["stopped_before_submit"] is True
    assert res["steps_completed"] >= 2
    assert {
        "country",
        "first_name",
        "last_name",
        "city",
        "phone_device",
        "phone",
        "cv_pdf",
        "source",
        "prev_employed",
    }.issubset(set(res["filled_fields"]))


def test_login_wall_guest_bypass_and_email_prefill_without_touching_password(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _sample_profile(tmp_path)
    job = _sample_job()
    kit = generate_application_kit(job, profile, fetch_email=False)

    # Case A: Login gate com opção "Continue as Guest"
    guest_html = tmp_path / "guest_wall.html"
    guest_html.write_text(
        """<!doctype html>
        <html><body>
          <div id="gate">
            <h2>Sign in to apply</h2>
            <input type="email" placeholder="Email">
            <input type="password" placeholder="Password">
            <button type="button" onclick="showForm()">Continue as Guest</button>
          </div>
          <form id="real-form" style="display:none">
            <input type="text" placeholder="Full name">
            <input type="email" placeholder="Email">
            <input type="file" name="resume" style="display:none">
          </form>
          <script>
            function showForm() {
              document.getElementById('gate').style.display = 'none';
              document.getElementById('real-form').style.display = 'block';
            }
          </script>
        </body></html>""",
        encoding="utf-8",
    )
    res_guest = run_autofill_browser(
        job,
        kit,
        profile,
        headless=True,
        keep_open=False,
        target_url=guest_html.as_uri(),
        user_data_dir=tmp_path / "chrome_guest",
    )
    assert res_guest["status"] == "done"
    assert res_guest["needs_login"] is False
    assert {"full_name", "email", "cv_pdf"}.issubset(set(res_guest["filled_fields"]))

    # Case B: Barreira de login Workday sem guest -> pré-preenche SÓ o email, nunca a password, e injeta botão HUD
    login_html = tmp_path / "strict_login_wall.html"
    login_html.write_text(
        """<!doctype html>
        <html><body>
          <div data-automation-id="signInContent">
            <h2>Sign In or Create Account</h2>
            <input id="login-email" type="email" data-automation-id="email" placeholder="Email Address">
            <input id="login-pw" type="password" placeholder="Password">
            <button type="button" data-automation-id="signInSubmitButton">Sign In</button>
          </div>
        </body></html>""",
        encoding="utf-8",
    )
    with sync_playwright() as p:
        context = _launch_persistent(p, tmp_path / "chrome_login_direct", headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_html.as_uri(), wait_until="domcontentloaded")
        res_login = _fill_current_page(
            page,
            context,
            job=job,
            kit=kit,
            profile=profile,
            cv_path=Path(kit["cv"]["path"]),
            values={"email": profile["email"]},
            follow_hops=True,
        )
        assert res_login["status"] == "needs_login"
        assert res_login["needs_login"] is True
        assert page.locator("#login-email").input_value() == "alex.sa@example.com"
        assert page.locator("#login-pw").input_value() == ""
        assert page.locator("#estagio-radar-hud-run").is_visible()
        context.close()


def test_ai_dom_unstuck_fallback_for_unknown_portal_and_submit_guard(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    profile = _sample_profile(tmp_path)
    job = _sample_job(company="GermanTech")
    kit = generate_application_kit(job, profile, fetch_email=False)

    class GoodMockGemini:
        class interactions:
            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(
                    output_text=json.dumps(
                        {"action": "click", "element_index": 0, "reason": "clicks unknown CTA"}
                    )
                )

    unknown_html = tmp_path / "unknown_portal.html"
    unknown_html.write_text(
        """<!doctype html>
        <html><body>
          <div id="gate">
            <h2>Karriereportal</h2>
            <p>Willkommen bei unserem Team in Lissabon.</p>
            <button id="start-de" type="button" onclick="openDeForm()">Jetzt loslegen →</button>
          </div>
          <form id="de-form" style="display:none">
            <input type="text" placeholder="Full name">
            <input type="email" placeholder="Email">
            <input type="file" name="resume" style="display:none">
          </form>
          <script>
            function openDeForm() {
              document.getElementById('gate').style.display = 'none';
              document.getElementById('de-form').style.display = 'block';
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
        target_url=unknown_html.as_uri(),
        user_data_dir=tmp_path / "chrome_ai_unstuck",
        ai_client=GoodMockGemini(),
    )
    assert res["status"] == "done"
    assert res["cv_attached"] is True
    assert {"full_name", "email", "cv_pdf"}.issubset(set(res["filled_fields"]))

    # Segurança: mesmo que o modelo tente clicar em Submit Application, o guard bloqueia!
    submit_guard_html = tmp_path / "submit_guard.html"
    submit_guard_html.write_text(
        """<!doctype html>
        <html><body>
          <script>window.__SUBMITTED = false;</script>
          <p>Final review screen before submission.</p>
          <button type="button" onclick="window.__SUBMITTED=true">Submit Application</button>
        </body></html>""",
        encoding="utf-8",
    )

    class BadSubmitGemini:
        class interactions:
            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(
                    output_text=json.dumps({"action": "click", "element_index": 0, "reason": "bad submit attempt"})
                )

    direct_choice = pick_unstuck_action_with_gemini(
        url="https://example.com",
        title="Test",
        headings="Review",
        body_excerpt="Review",
        clickables=[{"idx": 0, "tag": "button", "text": "Submit Application", "aria": "", "dataAuto": ""}],
        client=BadSubmitGemini(),
    )
    assert direct_choice["element_index"] == 0

    with sync_playwright() as p:
        context = _launch_persistent(p, tmp_path / "chrome_submit_guard", headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(submit_guard_html.as_uri(), wait_until="domcontentloaded")
        _follow_apply_hops(page, context, max_hops=2, allow_ai_unstuck=True, ai_client=BadSubmitGemini())
        assert page.evaluate("() => window.__SUBMITTED") is False
        context.close()


def test_dashboard_pna_cors_and_active_tab_fallback_fingerprint(tmp_path, monkeypatch):
    profile = _sample_profile(tmp_path)
    prof_path = tmp_path / "candidate_profile.yaml"
    prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")

    db = RadarStorage(tmp_path / "radar.db")
    job = _sample_job(fp="fp-v7-rev")
    db.upsert_jobs([job], now=NOW)

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
        opt_req = urllib.request.Request(f"{base}/api/autofill-active-tab", method="OPTIONS")
        with urllib.request.urlopen(opt_req, timeout=4) as opt_res:
            assert opt_res.headers.get("Access-Control-Allow-Private-Network") == "true"
            assert opt_res.headers.get("Access-Control-Allow-Origin") == "*"

        # Guardar payload inicial via /api/autofill
        req_first = urllib.request.Request(
            f"{base}/api/autofill",
            data=json.dumps({"fingerprint": "fp-v7-rev"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        r1 = json.loads(urllib.request.urlopen(req_first, timeout=4).read().decode("utf-8"))
        assert r1["ok"] is True

        # Botão HUD envia fingerprint vazio -> fallback automático para o último payload guardado!
        req_hud = urllib.request.Request(
            f"{base}/api/autofill-active-tab",
            data=json.dumps({"fingerprint": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        r2 = json.loads(urllib.request.urlopen(req_hud, timeout=4).read().decode("utf-8"))
        assert r2["ok"] is True
        assert len(spawned_cmds) == 2
        assert "--active-tab" in spawned_cmds[1]
        assert "fp-v7-rev" in spawned_cmds[1]
    finally:
        server.shutdown()
        server.server_close()
