"""Testes de regressão/recall v8.0: falsos negativos auditados, novos ATS, Turbo-Sniper ETag, circuit breaker e ATS discovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from estagio_radar.ats_discovery import (
    discover_and_persist_ats,
    extract_ats_board_from_url,
    load_discovered_ats,
    merge_sources_with_discovered,
)
from estagio_radar.filter_engine import evaluate_raw_job
from estagio_radar.models import (
    CompanyProfile,
    CompanyType,
    DomainCategory,
    GeoZone,
    RawJob,
    RoleTier,
)
from estagio_radar.scoring import compute_score
from estagio_radar.scrapers.ats_boards import (
    parse_personio_xml,
    parse_revolut_next_data,
    parse_workable_json,
    scrape_workday,
)
from estagio_radar.scrapers.base import DomainCircuitBreaker, ETagCache
from estagio_radar.scrapers.pt_portals import parse_itjobs_html

NOW = datetime(2026, 11, 28, 16, 0, tzinfo=timezone.utc)


def _raw(
    title: str,
    *,
    company: str = "Cloudflare",
    location: str = "Lisbon, Portugal",
    all_locations: list[str] | None = None,
    is_remote: bool = False,
    source: str = "Greenhouse (Cloudflare)",
    source_type: str = "ats",
    published_at: datetime | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
) -> RawJob:
    return RawJob(
        source=source,
        source_type=source_type,
        job_id="id-1",
        title=title,
        company=company,
        location=location,
        all_locations=all_locations or [location],
        job_url=f"https://example.com/{abs(hash(title))}",
        is_remote=is_remote,
        published_at=published_at,
        description=description,
        tags=tags or [],
    )


def test_filter_catches_audited_false_negatives_with_zero_false_positives():
    # 1. Bug do underscore: EDP Professional Internship_Data Science em Setubal/Lisboa
    edp_ds = _raw(
        "EDP Professional Internship_Data Science & AI",
        company="EDP",
        location="Setúbal, PT",
        source="SuccessFactors (EDP)",
        published_at=NOW - timedelta(days=3),
    )
    assert evaluate_raw_job(edp_ds, now=NOW).accepted is True

    # 2. Technology/IT Audit Trainee passa (nao financeiro/fiscal)
    siemens_cloud_audit = _raw(
        "Technology Audit Trainee (f/m/d)",
        company="Siemens",
        location="Amadora, Lisboa, Portugal",
        source="Siemens Avature",
        published_at=NOW - timedelta(days=3),
    )
    assert evaluate_raw_job(siemens_cloud_audit, now=NOW).accepted is True

    # 3. Rolling 2027 ATS internship com 75 dias aceita ate 90 dias
    rolling_intern = _raw(
        "Applied AI Intern 2027",
        published_at=NOW - timedelta(days=75),
    )
    assert evaluate_raw_job(rolling_intern, recency_days=60, now=NOW).accepted is True

    # 4. Vaga PT sem "Junior" no titulo mas com "0-2 years" / "recent graduates" na descricao
    hidden_grad = _raw(
        "Software Engineer, Agentic Systems",
        published_at=NOW - timedelta(days=2),
        description="Ideal for recent graduates with 0-2 years of experience in Python and distributed systems.",
    )
    dec_hidden = evaluate_raw_job(hidden_grad, now=NOW)
    assert dec_hidden.accepted is True
    assert dec_hidden.role_tier == RoleTier.ENTRY_JUNIOR

    # 5. Empresa remote-first elegivel na UE com location="Remote"
    sb_remote = _raw(
        "Software Engineer Intern",
        company="Supabase",
        location="Remote",
        source="Ashby (Supabase)",
        is_remote=True,
        published_at=NOW - timedelta(days=1),
    )
    assert evaluate_raw_job(sb_remote, now=NOW, remote_eu_friendly=False).accepted is False
    assert evaluate_raw_job(sb_remote, now=NOW, remote_eu_friendly=True).accepted is True

    # 6. Zero falsos positivos mantidos!
    for bad in [
        _raw("VAT Tax Trainee (m/f/d)", company="Siemens", location="Amadora, Lisboa, Portugal", published_at=NOW - timedelta(days=2)),
        _raw("Technology and Financial Audit focus Accounting Associate", company="Siemens", location="Amadora, Lisboa, Portugal", published_at=NOW - timedelta(days=2)),
        _raw("Senior Software Engineer", location="Lisbon, Portugal", published_at=NOW - timedelta(days=2)),
        _raw("Software Engineer Intern", location="Remote - United States", is_remote=True, published_at=NOW - timedelta(days=2)),
        _raw("Junior Python Developer", company="Deloitte", location="Lisbon, Portugal", published_at=NOW - timedelta(days=2)),
    ]:
        assert evaluate_raw_job(bad, now=NOW, remote_eu_friendly=True).accepted is False


def test_itjobs_page1_date_fallback_and_cv_stack_score_bonus():
    html = """
    <ul class="listing">
      <li>
        <div class="list-name"><a href="/empresa/acme">Acme Tech</a></div>
        <a class="title" href="/oferta/999001/junior-systems-administrator">Junior Systems Administrator</a>
        <span>Lisboa</span>
      </li>
    </ul>
    """
    assert parse_itjobs_html(html)[0].published_at is None
    jobs = parse_itjobs_html(html, fallback_now=NOW)
    assert len(jobs) == 1
    assert jobs[0].published_at == NOW
    assert evaluate_raw_job(jobs[0], now=NOW).accepted is True

    prof = CompanyProfile(company_name="Revolut", normalized_name="revolut", company_tier="S", bayesian_rating=4.2)
    base_score, _, base_badges = compute_score(
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.LISBOA_METRO,
        company=prof,
        published_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    boosted_score, _, boosted_badges = compute_score(
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.AI_ML,
        geo_zone=GeoZone.LISBOA_METRO,
        company=prof,
        published_at=NOW - timedelta(hours=1),
        now=NOW,
        title="Applied AI Intern",
        description="Work with Python, PyTorch, JAX, and LLM evaluation pipelines.",
    )
    assert "🎯 STACK MATCH" not in base_badges
    assert "🎯 STACK MATCH" in boosted_badges
    assert boosted_score >= base_score


def test_parsers_revolut_workable_personio_and_workday_pagination():
    rev_payload = {
        "props": {
            "pageProps": {
                "positions": [
                    {
                        "id": "rev-ai-2027",
                        "text": "Graduate Programme 2027: Applied AI Engineer",
                        "locations": [
                            {"name": "Lisbon", "type": "office", "country": "Portugal"},
                            {"name": "Portugal - Remote", "type": "remote", "country": "Portugal"},
                        ],
                        "team": "Engineering",
                        "description": "<p>Python and PyTorch ML systems.</p>",
                    }
                ]
            }
        }
    }
    rev_html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(rev_payload)}</script></body></html>'
    rev_jobs = parse_revolut_next_data(rev_html, now=NOW)
    assert len(rev_jobs) == 1
    assert rev_jobs[0].job_url == "https://www.revolut.com/careers/apply/rev-ai-2027/"
    assert evaluate_raw_job(rev_jobs[0], now=NOW).accepted is True

    wk_jobs = parse_workable_json(
        {
            "results": [
                {
                    "shortcode": "ABC123",
                    "title": "Machine Learning Intern",
                    "remote": True,
                    "location": {"city": "Lisbon", "country": "Portugal"},
                    "published": "2026-11-27T10:00:00Z",
                    "department": ["Research"],
                }
            ]
        },
        "huggingface",
        "Hugging Face",
    )
    assert len(wk_jobs) == 1
    assert wk_jobs[0].job_url == "https://apply.workable.com/huggingface/j/ABC123/"

    per_xml = """<?xml version="1.0" encoding="utf-8"?>
    <workzag-jobs>
      <position>
        <id>7788</id>
        <name>Software Engineer Intern</name>
        <office>Lisbon, Portugal</office>
        <department>Engineering</department>
        <employmentType>intern</employmentType>
        <createdAt>2026-11-27T09:00:00+00:00</createdAt>
      </position>
    </workzag-jobs>"""
    per_jobs = parse_personio_xml(per_xml, "acme", "Acme")
    assert len(per_jobs) == 1
    assert per_jobs[0].job_url == "https://acme.jobs.personio.de/job/7788"


@pytest.mark.asyncio
async def test_workday_multipage_pagination_with_monkeypatch(monkeypatch):
    import sys

    class FakeResp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    offsets_seen = []

    class FakeCurlSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            off = (kwargs.get("json") or {}).get("offset", 0)
            offsets_seen.append(off)
            total = 45
            batch = min(20, max(0, total - off))
            return FakeResp(
                {
                    "total": total,
                    "jobPostings": [
                        {
                            "title": f"Software Intern {off + i}",
                            "externalPath": f"/job/Lisbon/Intern_{off + i}",
                            "locationsText": "Lisbon, Portugal",
                            "postedOn": "Posted Today",
                        }
                        for i in range(batch)
                    ],
                }
            )

    monkeypatch.setitem(sys.modules, "curl_cffi.requests", SimpleNamespace(AsyncSession=FakeCurlSession))
    cb = DomainCircuitBreaker()
    res = await scrape_workday(
        [{"tenant": "richemont", "wd": "wd3", "site": "richemont", "company": "Richemont"}],
        httpx.AsyncClient(),
        circuit_breaker=cb,
    )
    assert offsets_seen == [0, 20, 40]
    assert len(res.jobs) == 45


def test_persistent_etag_cache_and_circuit_breaker(tmp_path):
    cache_path = tmp_path / "etag_cache.json"
    c1 = ETagCache(persist_path=cache_path)
    sample = [_raw("AI Intern", published_at=NOW)]
    c1.store("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs", {"etag": '"abc123"', "last-modified": "Fri"}, sample)
    c1.save()
    assert cache_path.exists()

    c2 = ETagCache(persist_path=cache_path)
    assert c2.request_headers("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs") == {
        "If-None-Match": '"abc123"',
        "If-Modified-Since": "Fri",
    }
    reloaded = c2.get_cached_jobs("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs")
    assert len(reloaded) == 1 and reloaded[0].title == "AI Intern"

    cb = DomainCircuitBreaker()
    assert cb.is_open("linkedin.com") is False
    cd1 = cb.record_rate_limit("linkedin.com", base_cooldown=60.0)
    assert cd1 == 60.0
    assert cb.is_open("linkedin.com") is True
    cb.record_success("linkedin.com")
    assert cb.is_open("linkedin.com") is False


@pytest.mark.asyncio
async def test_ats_auto_discovery_extracts_validates_and_merges(tmp_path):
    assert extract_ats_board_from_url("https://boards.greenhouse.io/newco/jobs/123", "NewCo")["ats_type"] == "greenhouse"
    assert extract_ats_board_from_url("https://jobs.ashbyhq.com/voodoo/abc")["ats_type"] == "ashby"
    rich = extract_ats_board_from_url("https://richemont.wd3.myworkdayjobs.com/pt-BR/richemont/job/Tech_1/apply")
    assert rich == {"ats_type": "workday", "tenant": "richemont", "wd": "wd3", "site": "richemont", "company": "Richemont"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "newco" in str(request.url):
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Intern"}]})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    disc_file = tmp_path / "discovered_ats.json"
    base_cfg = {"greenhouse": [{"slug": "cloudflare", "company": "Cloudflare"}]}

    async with httpx.AsyncClient(transport=transport) as client:
        added = await discover_and_persist_ats(
            [
                ("https://boards.greenhouse.io/cloudflare/jobs/1", "Cloudflare"),  # ja conhecido -> skip
                ("https://boards.greenhouse.io/newco/jobs/99", "NewCo"),         # valido -> add
                ("https://boards.greenhouse.io/deadco/jobs/1", "DeadCo"),        # 404 -> rejeitado
            ],
            base_cfg,
            cache_path=disc_file,
            client=client,
        )

    assert len(added) == 1 and added[0]["slug"] == "newco"
    saved = load_discovered_ats(disc_file)
    merged = merge_sources_with_discovered(base_cfg, saved)
    assert [x["slug"] for x in merged["greenhouse"]] == ["cloudflare", "newco"]
