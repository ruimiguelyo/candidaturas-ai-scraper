"""Testes de deduplicação, SQLite, parsers dos scrapers, exportadores, dashboard e notificadores."""

import json
from datetime import datetime, timezone

from estagio_radar.company_intel import CompanyIntelService
from estagio_radar.dashboard import make_handler
from estagio_radar.dedup import canonicalize_url, deduplicate_accepted
from estagio_radar.exporters import export_all_artifacts, render_dashboard_html, safe_csv_cell
from estagio_radar.filter_engine import evaluate_raw_job
from estagio_radar.models import CompanyProfile, CompanyType, DomainCategory, EnrichedJob, GeoZone, RawJob, RoleTier, ScoreBreakdown
from estagio_radar.notifiers.discord_notifier import build_discord_payload
from estagio_radar.notifiers.email_notifier import generate_email_html
from estagio_radar.notifiers.ntfy_notifier import build_ntfy_headers_and_body
from estagio_radar.notifiers.telegram_notifier import format_telegram_message
from estagio_radar.scrapers.linkedin import parse_linkedin_cards
from estagio_radar.scrapers.pt_portals import (
    decode_net_empregos_bytes,
    parse_itjobs_html,
    parse_landing_jobs_items,
    parse_net_empregos_html,
)
from estagio_radar.scrapers.teamlyzer_jobs import parse_teamlyzer_job_cards
from estagio_radar.storage import RadarStorage

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def test_dedup_prefers_direct_ats_over_linkedin():
    gh_job = RawJob(
        source="Greenhouse (Cloudflare)",
        source_type="ats",
        job_id="8300",
        title="Software Engineer Intern (2027)",
        company="Cloudflare",
        location="Lisbon, Portugal",
        job_url="https://boards.greenhouse.io/cloudflare/jobs/8300?gh_jid=8300&utm_source=x",
        published_at=NOW,
    )
    li_job = RawJob(
        source="LinkedIn",
        source_type="linkedin",
        job_id="4341",
        title="Software Engineer Intern (2027)",
        company="Cloudflare",
        location="Lisbon, Portugal",
        job_url="https://pt.linkedin.com/jobs/view/software-engineer-intern-4341?trk=public",
        published_at=NOW,
    )
    d1 = evaluate_raw_job(gh_job, now=NOW)
    d2 = evaluate_raw_job(li_job, now=NOW)
    merged = deduplicate_accepted([(li_job, d2), (gh_job, d1)])
    assert len(merged) == 1
    assert merged[0]["job"].source == "Greenhouse (Cloudflare)"
    assert "boards.greenhouse.io/cloudflare/jobs/8300" in merged[0]["canonical_url"]
    assert "gh_jid=" not in merged[0]["canonical_url"]
    assert "Greenhouse (Cloudflare)" in merged[0]["sources"]
    assert "LinkedIn" in merged[0]["sources"]


def test_canonicalize_url_strips_tracking():
    url = "https://example.com/jobs/123/?utm_source=li&trk=abc&keep=1"
    assert canonicalize_url(url) == "https://example.com/jobs/123?keep=1"


def test_sqlite_storage_upsert_new_vs_notified(tmp_path):
    db = RadarStorage(tmp_path / "radar.db")
    job = EnrichedJob(
        fingerprint="fp1",
        title="AI Engineer Intern",
        company="Feedzai",
        location="Lisboa, Portugal",
        job_url="https://boards.greenhouse.io/feedzai/jobs/1",
        canonical_url="https://boards.greenhouse.io/feedzai/jobs/1",
        sources=["Greenhouse (Feedzai)"],
        primary_source="Greenhouse (Feedzai)",
        source_type="ats",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.LISBOA_METRO,
        published_at=NOW,
        published_date="2026-11-24",
        freshness_label="Há 1h",
        company_profile=CompanyProfile(
            company_name="Feedzai",
            normalized_name="feedzai",
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=92.0,
        score_breakdown=ScoreBreakdown(total=92.0, role_points=30, company_points=25, location_points=17, freshness_points=20),
        badges=["🏆 TIER S", "🤖 IA/ML"],
    )

    _, new1 = db.upsert_jobs([job], now=NOW)
    assert len(new1) == 1
    db.mark_notified(["fp1"], now=NOW)

    # Segunda execução: já foi notificada -> new2 deve ser vazio
    _, new2 = db.upsert_jobs([job], now=NOW)
    assert len(new2) == 0

    assert db.update_job_status("fp1", "applied", "Enviei CV hoje")
    listed = db.list_jobs()
    assert listed[0].user_status == "applied"
    assert listed[0].user_notes == "Enviei CV hoje"


def test_parsers_html_and_cp1252():
    tl_html = """
    <div class="panel jobcard">
      <a class="jobcard__title-link" href="/companies/feedzai/job/ai-intern-999">AI Engineering Intern</a>
      <a class="jobcard__company" href="/companies/feedzai">Feedzai</a>
      <span class="jobcard__rating">4.1</span>
      <span class="jobcard__reviews">85 Reviews</span>
      <span class="jobcard__location">Lisboa (Regime: Híbrido)</span>
      <span class="jobcard__seniority">Estágio</span>
      <span class="jobcard__on-site" title="No site há 2 horas">Há 2 horas</span>
      <span class="jobcard__tag">python</span>
    </div>
    """
    tl_jobs = parse_teamlyzer_job_cards(tl_html)
    assert len(tl_jobs) == 1
    assert tl_jobs[0].company == "Feedzai"
    assert tl_jobs[0].teamlyzer_hint["rating"] == 4.1

    li_html = """
    <ul><li>
      <h3 class="base-search-card__title">Machine Learning Intern</h3>
      <h4 class="base-search-card__subtitle">Cloudflare</h4>
      <span class="job-search-card__location">Lisbon, Portugal</span>
      <a class="base-card__full-link" href="https://pt.linkedin.com/jobs/view/ml-intern-987654?trk=abc">link</a>
      <time datetime="2026-11-24">25 minutes ago</time>
    </li></ul>
    """
    li_jobs = parse_linkedin_cards(li_html)
    assert len(li_jobs) == 1
    assert li_jobs[0].published_raw == "25 minutes ago"

    it_html = """
    <div class="heading">Hoje</div>
    <ul class="listing"><li>
      <a class="title" href="/oferta/555111/estagio-programacao-setubal">Estágio Programação IA</a>
      <a href="/empresa/visteon" title="Visteon">Visteon</a>
      <div>Palmela / Setúbal - Híbrido - €15 000</div>
    </li></ul>
    """
    it_jobs = parse_itjobs_html(it_html)
    assert len(it_jobs) == 1
    assert it_jobs[0].location == "Setúbal, Portugal"

    lj_jobs = parse_landing_jobs_items([{
        "id": 77,
        "title": "Junior AI Software Engineer",
        "url": "https://landing.jobs/at/critical-techworks/junior-ai-77",
        "locations": [{"city": "Lisbon", "country_code": "PT"}],
        "remote": False,
        "tags": ["Python", "AI"],
        "published_at": "2026-11-24T10:00:00Z",
    }])
    assert lj_jobs[0].company == "Critical Techworks"

    ne_raw = (
        '<div class="job-item"><h2><a class="oferta-link" href="/16240999/estagio-ia-setubal/">'
        'Estágio Informática / IA</a></h2><div class="job-ad-item"><ul>'
        '<li><i class="flaticon-calendar"></i> 24-11-2026</li>'
        '<li><i class="flaticon-pin"></i> Setúbal</li>'
        '<li><i class="fa fa-tags"></i> Informática ( Programação )</li>'
        '<li><i class="flaticon-work"></i> Tech Palmela Lda</li>'
        '</ul></div></div>'
    ).encode("cp1252")
    ne_html = decode_net_empregos_bytes(ne_raw, "text/html; charset=iso-8859-1")
    ne_jobs = parse_net_empregos_html(ne_html, default_zone="Setúbal")
    assert ne_jobs[0].title == "Estágio Informática / IA"
    assert ne_jobs[0].company == "Tech Palmela Lda"


def test_exporters_notifiers_and_csv_safety(tmp_path):
    assert safe_csv_cell("=CMD()") == "'=CMD()"
    job = EnrichedJob(
        fingerprint="fp2",
        title="Data Science Intern </script>",
        company="Riskified",
        location="Lisboa, Portugal",
        job_url="https://boards.greenhouse.io/riskified/jobs/42",
        canonical_url="https://boards.greenhouse.io/riskified/jobs/42",
        sources=["Greenhouse (Riskified)"],
        primary_source="Greenhouse (Riskified)",
        source_type="ats",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.DATA,
        geo_zone=GeoZone.LISBOA_METRO,
        published_at=NOW,
        published_date="2026-11-24",
        freshness_label="Há 1h",
        company_profile=CompanyProfile(
            company_name="Riskified",
            normalized_name="riskified",
            teamlyzer_rating=4.1,
            teamlyzer_reviews=19,
            avg_salary_eur=74620,
            company_tier="S",
            company_type=CompanyType.PRODUCT_TECH,
        ),
        score=94.0,
        score_breakdown=ScoreBreakdown(total=94.0, role_points=30, company_points=27, location_points=17, freshness_points=20),
        badges=["🔥 NOVO", "🚆 LISBOA", "🏆 TIER S"],
    )

    arts = export_all_artifacts([job], [{"source": "Test", "reason_code": "non_tech_role", "title": "=bad"}], tmp_path)
    assert arts["csv"].exists()
    assert arts["json"].exists()
    assert arts["dashboard"].exists()
    html = render_dashboard_html([job], interactive=True)
    assert "</script>" not in html.split("let JOBS = ")[1].split(";")[0]

    assert build_discord_payload([job])["embeds"][0]["title"].startswith("[94/100]")
    assert "Riskified" in format_telegram_message([job])
    hdrs, body = build_ntfy_headers_and_body(job)
    assert hdrs["Click"] == job.job_url
    assert "Riskified" in body
    assert "Riskified" in generate_email_html([job])

    db = RadarStorage(tmp_path / "ui.db")
    db.upsert_jobs([job], now=NOW)

    # Smoke test HTTP real do servidor dashboard local
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html_resp = urllib.request.urlopen(f"{base}/", timeout=5).read().decode("utf-8")
        assert "Estágio Radar PT" in html_resp

        jobs_resp = json.loads(urllib.request.urlopen(f"{base}/api/jobs", timeout=5).read().decode("utf-8"))
        assert jobs_resp["count"] == 1

        stats_resp = json.loads(urllib.request.urlopen(f"{base}/api/stats", timeout=5).read().decode("utf-8"))
        assert stats_resp["total_active"] == 1

        req = urllib.request.Request(
            f"{base}/api/status",
            data=json.dumps({"fingerprint": "fp2", "status": "saved", "notes": "Top"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        post_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
        assert post_resp["ok"] is True
        assert db.list_jobs()[0].user_status == "saved"
    finally:
        server.shutdown()
        server.server_close()


def test_dedup_pipe_brand_and_itjobs_nospace_and_teamlyzer_nova():
    sr_job = RawJob(
        source="SmartRecruiters (Devoteam)",
        source_type="ats",
        job_id="744",
        title="Devoteam Cyber Trust | Cybersecurity Engineer (IAM) | IEFP Internship",
        company="Devoteam",
        location="Lisboa, Lisboa, Portugal",
        job_url="https://jobs.smartrecruiters.com/Devoteam/744",
        published_at=NOW,
    )
    li_job = RawJob(
        source="LinkedIn",
        source_type="linkedin",
        job_id="999",
        title="Devoteam Cyber Trust | Cybersecurity Engineer (IAM) | IEFP Internship",
        company="Devoteam | Cyber Trust",
        location="Lisbon, Lisbon, Portugal",
        job_url="https://pt.linkedin.com/jobs/view/999",
        published_at=NOW,
    )
    merged = deduplicate_accepted([
        (li_job, evaluate_raw_job(li_job, now=NOW)),
        (sr_job, evaluate_raw_job(sr_job, now=NOW)),
    ])
    assert len(merged) == 1
    assert merged[0]["job"].source == "SmartRecruiters (Devoteam)"

    it_html = """
    <div class="heading">19nov</div>
    <ul class="listing"><li>
      <a class="title" href="/oferta/519/junior-dev">Junior Python Developer</a>
      <div class="list-name"><a href="/empresa/acme" title="Acme PT">Acme PT</a></div>
      <div>Lisboa - Híbrido</div>
    </li></ul>
    """
    it_jobs = parse_itjobs_html(it_html)
    assert len(it_jobs) == 1
    assert it_jobs[0].company == "Acme PT"
    assert it_jobs[0].published_at is not None and it_jobs[0].published_at.day == 19

    tl_nova_html = """
    <div class="panel jobcard">
      <a class="jobcard__title-link" href="/companies/acme/job/ai-intern-1">AI Intern</a>
      <a class="jobcard__company" href="/companies/acme">Acme</a>
      <span class="jobcard__location">Lisboa (Regime: Híbrido)</span>
      <span class="badge">Nova</span>
    </div>
    """
    tl_jobs = parse_teamlyzer_job_cards(tl_nova_html)
    assert len(tl_jobs) == 1
    assert tl_jobs[0].published_at is not None
