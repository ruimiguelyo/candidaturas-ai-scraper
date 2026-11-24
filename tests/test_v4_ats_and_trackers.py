"""Testes das novas fontes e regras v4.0 (Siemens, TEKEVER, Nokia, SuccessFactors, EU Trackers, EURAXESS, ETagCache)."""

from datetime import datetime, timezone

import httpx
import pytest

from estagio_radar.filter_engine import evaluate_raw_job
from estagio_radar.models import GeoZone, RawJob, RoleTier
from estagio_radar.scrapers.ats_boards import (
    parse_oracle_hcm_json,
    parse_siemens_avature_html,
    parse_successfactors_html,
    parse_teamtailor_rss,
    scrape_greenhouse,
)
from estagio_radar.scrapers.base import ETagCache
from estagio_radar.scrapers.eu_trackers import (
    parse_cavlero_markdown,
    parse_emjumaev_markdown,
    parse_euraxess_html,
)

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def test_teamtailor_nested_locations_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:tt="https://teamtailor.com/locations">
      <channel>
        <item>
          <title>Curricular Internships 2027</title>
          <link>https://careers.tekever.com/jobs/8536848-curricular-internships-2027</link>
          <pubDate>Tue, 24 Nov 2026 10:00:00 +0000</pubDate>
          <description>&lt;p&gt;Software engineering and AI.&lt;/p&gt;</description>
          <tt:locations>
            <tt:location>
              <tt:name>(PT) Tekever Lisboa</tt:name>
              <tt:city>Lisboa</tt:city>
              <tt:country>Portugal</tt:country>
            </tt:location>
          </tt:locations>
        </item>
      </channel>
    </rss>"""
    jobs = parse_teamtailor_rss(xml, "TEKEVER")
    assert len(jobs) == 1
    assert jobs[0].location == "Lisboa, Portugal"
    dec = evaluate_raw_job(jobs[0], now=NOW, extra_tech_company_override=True)
    assert dec.accepted
    assert dec.geo_zone == GeoZone.LISBOA_METRO
    assert dec.role_tier == RoleTier.INTERNSHIP


def test_siemens_avature_and_manager_intern_override():
    html = """
    <article class="article--result">
      <h3><a href="/en_US/externaljobs/JobDetail/526925">Internships - Application Manager (m/f/d)</a></h3>
      <div class="article__header__text__subtitle">Amadora , Lisboa , Portugal • Job ID: 526925 • IT</div>
    </article>
    <article class="article--result">
      <h3><a href="/en_US/externaljobs/JobDetail/527474">Curricular Internship | Digital Solutions &amp; Automation (m/f/d)</a></h3>
      <div class="article__header__text__subtitle">Amadora , Lisboa , Portugal • Job ID: 527474 • Internal Services</div>
    </article>
    """
    jobs = parse_siemens_avature_html(html, now=NOW)
    assert len(jobs) == 2
    assert jobs[0].location == "Amadora, Lisboa, Portugal"
    for j in jobs:
        dec = evaluate_raw_job(j, now=NOW)
        assert dec.accepted, f"Siemens devia ser aceite: {j.title} ({dec.reason_detail})"
        assert dec.geo_zone == GeoZone.LISBOA_METRO


def test_successfactors_and_oracle_hcm_parsers():
    sf_html = """
    <table><tr class="data-row">
      <td><a class="jobTitle-link" href="/job/Lisboa-EDP-Professional-Internship/123/">EDP Professional Internship 2026 - Software &amp; Data</a></td>
      <td><span class="jobLocation">Lisboa, PT</span></td>
      <td><span class="jobDate">Nov 17, 2026</span></td>
    </tr></table>
    """
    sf_jobs = parse_successfactors_html(sf_html, "https://jobs.edp.com/search/", "EDP")
    assert len(sf_jobs) == 1
    assert sf_jobs[0].published_at is not None and sf_jobs[0].published_at.month == 11
    assert evaluate_raw_job(sf_jobs[0], now=NOW).accepted

    nokia_payload = {
        "items": [{
            "requisitionList": [
                {
                    "Id": "9001",
                    "Title": "Cloud Network Intern",
                    "PrimaryLocation": "Amadora, Portugal",
                    "PostedDate": "2026-11-20",
                },
                {
                    "Id": "9002",
                    "Title": "Software Intern",
                    "PrimaryLocation": "Bangalore, India",
                    "PostedDate": "2026-11-20",
                },
            ]
        }]
    }
    nk_jobs = parse_oracle_hcm_json(nokia_payload, "https://nokia.example/job", "Nokia")
    assert len(nk_jobs) == 1
    assert nk_jobs[0].title == "Cloud Network Intern"
    assert evaluate_raw_job(nk_jobs[0], now=NOW).accepted


def test_github_trackers_and_euraxess_filters():
    cavlero_md = """
    | Company | Title | Location | Link | Date Posted |
    | ------- | ---- | -------- | ---------------- | ----------- |
    | **[Richemont](http://www.richemont.com)** | Professional Internship - Tech | Moscavide, Portugal | [Apply](https://careers.richemont.com/en/jobs/123) | |
    | **[Revolut](https://revolut.com)** | Internship Programme 2027: Technology Services Specialist (IT Systems Engineer) | Remoto, Portugal | [Apply](https://revolut.com/jobs/456) | Nov 18 |
    """
    c_jobs = parse_cavlero_markdown(cavlero_md, now=NOW)
    assert len(c_jobs) == 2
    assert evaluate_raw_job(c_jobs[0], now=NOW).geo_zone == GeoZone.LISBOA_METRO
    assert evaluate_raw_job(c_jobs[1], now=NOW).geo_zone == GeoZone.REMOTE_PT

    emj_md = """
    ## Visa
    | Role | Category | Degree | Location | Posted | First seen |
    |---|---|---|---|---|---|
    | [Consulting & Analytics Summer Intern](https://visa.wd5.myworkdayjobs.com/en-US/Visa/job/PT---Lisbon-Portugal/Intern_1) 🆕 | Data | — | PT - Lisbon, Portugal | 2026-11-18 |
    """
    e_jobs = parse_emjumaev_markdown(emj_md, now=NOW)
    assert len(e_jobs) == 1
    assert e_jobs[0].company == "Visa"
    assert evaluate_raw_job(e_jobs[0], now=NOW).accepted

    eur_html = """
    <article class="ecl-content-item">
      <h3><a href="/jobs/111">Research Fellowship (BI) - Machine Learning &amp; Computer Vision</a></h3>
      <div>FCT NOVA - Monte da Caparica | Posted on: 24 November 2026 | Bolsa de investigação em inteligência artificial.</div>
    </article>
    <article class="ecl-content-item">
      <h3><a href="/jobs/222">Postdoctoral Research Fellowship in AI</a></h3>
      <div>IST-ID Lisboa | Posted on: 24 November 2026 | Postdoc position.</div>
    </article>
    """
    eur_jobs = parse_euraxess_html(eur_html, now=NOW)
    assert len(eur_jobs) == 2
    d_bi = evaluate_raw_job(eur_jobs[0], now=NOW)
    d_postdoc = evaluate_raw_job(eur_jobs[1], now=NOW)
    assert d_bi.accepted and d_bi.geo_zone == GeoZone.SETUBAL_LOCAL
    assert not d_postdoc.accepted and d_postdoc.reason_code == "excluded_seniority_title"


def test_bosch_ai_sales_and_all_portugal_flag():
    bosch_lis = RawJob(
        source="SmartRecruiters (Bosch)",
        source_type="ats",
        job_id="b1",
        title="Extracurricular Internship | Functional Consulting – AI Sales Solutions (f/m/div.)",
        company="Bosch",
        location="Lisboa, Lisboa, Portugal",
        job_url="https://jobs.smartrecruiters.com/BoschGroup/b1",
        published_at=NOW,
    )
    assert evaluate_raw_job(bosch_lis, now=NOW).accepted

    # Sales Analytics Intern continua bloqueado!
    sales_analytics = bosch_lis.model_copy(update={"title": "Sales Analytics Intern"})
    assert not evaluate_raw_job(sales_analytics, now=NOW).accepted

    # Bosch Braga só passa quando --all-portugal está ativo
    bosch_braga = bosch_lis.model_copy(
        update={"title": "Academic Internship in Computer Science (f/m/div.)", "location": "Braga, Portugal"}
    )
    assert not evaluate_raw_job(bosch_braga, now=NOW, include_all_portugal=False).accepted
    dec_all = evaluate_raw_job(bosch_braga, now=NOW, include_all_portugal=True)
    assert dec_all.accepted and dec_all.geo_zone == GeoZone.PT_OTHER_CITY


@pytest.mark.asyncio
async def test_etag_cache_handles_304_not_modified():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={
                    "jobs": [{
                        "id": 99,
                        "title": "Software Engineer Intern",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/99",
                        "location": {"name": "Lisbon, Portugal"},
                        "first_published": "2026-11-24T12:00:00Z",
                    }]
                },
            )
        assert request.headers.get("If-None-Match") == '"v1"'
        return httpx.Response(304)

    cache = ETagCache()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res1 = await scrape_greenhouse([{"slug": "acme", "company": "Acme"}], client, etag_cache=cache)
        res2 = await scrape_greenhouse([{"slug": "acme", "company": "Acme"}], client, etag_cache=cache)

    assert len(res1.jobs) == 1
    assert len(res2.jobs) == 1
    assert res2.health.requests_ok == 1
    assert res2.jobs[0].title == "Software Engineer Intern"


def test_euraxess_non_it_bi_rejected_and_caldas_filtered():
    eur_html = """
    <article class="ecl-content-item">
      <h3><a href="/jobs/333">Research Fellowship (BI) (Reference ICETA 2026-02)</a></h3>
      <div>ICETA - Instituto de Ciências, Tecnologias e Agroambiente da Universidade do Porto | Posted on: 24 November 2026 | Apoio técnico em química analítica.</div>
    </article>
    <article class="ecl-content-item">
      <h3><a href="/jobs/444">Call for Applications for a Research Grant (BI) - INNOREV</a></h3>
      <div>Instituto Politécnico de Bragança | Posted on: 24 November 2026 | Projeto de sustentabilidade.</div>
    </article>
    <article class="ecl-content-item">
      <h3><a href="/jobs/555">Public notice for one research grant Refª BI|2026/973 Project MALDIBANK</a></h3>
      <div>INESC ID | Posted on: 24 November 2026 | Lisbon, Portugal.</div>
    </article>
    """
    jobs = parse_euraxess_html(eur_html, now=NOW)
    assert jobs[0].location == "Porto, Portugal"
    assert jobs[1].location == "Bragança, Portugal"
    assert jobs[2].location == "Lisboa, Portugal"

    assert not evaluate_raw_job(jobs[0], now=NOW).accepted
    assert not evaluate_raw_job(jobs[1], now=NOW).accepted
    d_inesc = evaluate_raw_job(jobs[2], now=NOW, extra_tech_company_override=True)
    assert d_inesc.accepted and d_inesc.geo_zone == GeoZone.LISBOA_METRO

    caldas = RawJob(
        source="EU Tracker (cavlero)",
        source_type="ats",
        job_id="c1",
        title="Thesis Internships 2027",
        company="TEKEVER",
        location="Caldas da Rainha, Portugal",
        job_url="https://careers.tekever.com/jobs/1",
        published_at=NOW,
    )
    assert not evaluate_raw_job(caldas, now=NOW, include_all_portugal=False, extra_tech_company_override=True).accepted
    assert evaluate_raw_job(caldas, now=NOW, include_all_portugal=True, extra_tech_company_override=True).geo_zone == GeoZone.PT_OTHER_CITY
