import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.arbeitnow import ArbeitnowScraper
from scrapers.http_utils import get_with_retry
from scrapers.itjobs import ITJobsScraper
from scrapers.landing_jobs import LandingJobsScraper
from scrapers.linkedin import LinkedInScraper
from scrapers.remoteok import RemoteOKScraper


class FakeResponse:
    def __init__(self, status_code=200, *, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON fixture")
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("Unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def linkedin_page(job_id="1", title="Junior AI Engineer"):
    return f"""
    <ul><li>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle">Acme</h4>
      <span class="job-search-card__location">Lisbon, Portugal</span>
      <a class="base-card__full-link" href="https://linkedin.com/jobs/view/acme-{job_id}?trk=x">Open</a>
    </li></ul>
    """


def itjobs_page(job_id="1", title="Junior AI Engineer"):
    return f"""
    <article>
      <a class="title" href="/oferta/{job_id}/junior-ai-engineer">{title}</a>
      <a href="/empresa/acme" title="Acme"></a>
      <span>Lisboa</span>
    </article>
    """


def landing_card(job_id, title):
    return f"""
    <article>
      <a class="job-card__title" href="/jobs/{job_id}">{title}</a>
      <span class="job-card__company">Acme</span>
      <span class="job-card__location">Portugal</span>
    </article>
    """


def arbeitnow_job(slug, title, description=""):
    return {
        "slug": slug,
        "title": title,
        "description": description,
        "company_name": "Acme",
        "location": "Portugal",
        "url": f"https://www.arbeitnow.com/jobs/{slug}",
        "remote": True,
        "tags": [],
    }


class TestScraperCollectionV2(unittest.TestCase):
    def test_retry_only_transient_http_statuses(self):
        async def check():
            client = FakeClient(
                [FakeResponse(429), FakeResponse(503), FakeResponse(200, payload={})]
            )
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()) as sleep:
                response = await get_with_retry(client.get, "https://jobs.example")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(sleep.await_count, 2)

            non_transient = FakeClient([FakeResponse(400)])
            response = await get_with_retry(non_transient.get, "https://jobs.example")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(len(non_transient.calls), 1)

            timeout_then_success = FakeClient(
                [TimeoutError("temporary timeout"), FakeResponse(200, payload={})]
            )
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()):
                response = await get_with_retry(
                    timeout_then_success.get,
                    "https://jobs.example",
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(timeout_then_success.calls), 2)

        asyncio.run(check())

    def test_retry_honours_retry_after_and_curl_code_28(self):
        class CurlTimeout(Exception):
            code = 28

        async def check():
            rate_limited = FakeClient(
                [
                    FakeResponse(429, headers={"Retry-After": "7"}),
                    FakeResponse(200, payload={}),
                ]
            )
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()) as sleep:
                await get_with_retry(rate_limited.get, "https://jobs.example")
            self.assertEqual(sleep.await_args.args[0], 7.0)

            timed_out = FakeClient(
                [CurlTimeout("curl: (28) operation timed out"), FakeResponse(200, payload={})]
            )
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()):
                response = await get_with_retry(timed_out.get, "https://jobs.example")
            self.assertEqual(response.status_code, 200)

        asyncio.run(check())

    def test_linkedin_preserves_first_page_when_later_page_fails(self):
        client = FakeClient(
            [
                FakeResponse(200, text=linkedin_page()),
                FakeResponse(500),
                FakeResponse(500),
                FakeResponse(500),
            ]
        )
        with patch("scrapers.linkedin.AsyncSession", return_value=client):
            with patch("scrapers.linkedin.asyncio.sleep", new=AsyncMock()):
                jobs = asyncio.run(
                    LinkedInScraper().fetch("Junior AI", "Portugal", total_wanted=2)
                )

        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertEqual(len(client.calls), 4)
        self.assertTrue(jobs.partial)
        self.assertIn("start=10", jobs.warning)

    def test_linkedin_does_not_hide_first_page_failure(self):
        client = FakeClient([FakeResponse(400)])
        with patch("scrapers.linkedin.AsyncSession", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                asyncio.run(LinkedInScraper().fetch("Junior AI", "Portugal"))
        self.assertEqual(len(client.calls), 1)

    def test_itjobs_404_ends_pagination(self):
        client = FakeClient(
            [FakeResponse(200, text=itjobs_page()), FakeResponse(404)]
        )
        with patch("scrapers.itjobs.AsyncSession", return_value=client):
            jobs = asyncio.run(ITJobsScraper().fetch("junior", max_pages=3))

        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertFalse(jobs.partial)
        self.assertEqual(len(client.calls), 2)

    def test_itjobs_preserves_results_after_retried_later_failure(self):
        client = FakeClient(
            [
                FakeResponse(200, text=itjobs_page()),
                FakeResponse(503),
                FakeResponse(503),
                FakeResponse(503),
            ]
        )
        with patch("scrapers.itjobs.AsyncSession", return_value=client):
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()):
                jobs = asyncio.run(ITJobsScraper().fetch("junior", max_pages=2))

        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertTrue(jobs.partial)

    def test_landing_jobs_paginates_deduplicates_and_keeps_partial_results(self):
        page_one = landing_card("one", "Junior AI Engineer")
        page_two = landing_card("one", "Junior AI Engineer") + landing_card(
            "two", "Machine Learning Intern"
        )
        client = FakeClient(
            [
                FakeResponse(200, text=page_one),
                FakeResponse(200, text=page_two),
                FakeResponse(500),
                FakeResponse(500),
                FakeResponse(500),
            ]
        )
        with patch("scrapers.landing_jobs.httpx.AsyncClient", return_value=client):
            with patch("scrapers.http_utils.asyncio.sleep", new=AsyncMock()):
                jobs = asyncio.run(
                    LandingJobsScraper().fetch("ai", limit=10, max_pages=3)
                )

        self.assertEqual([job.job_id for job in jobs], ["one", "two"])
        self.assertTrue(jobs.partial)
        requested_pages = [call["kwargs"]["params"]["page"] for call in client.calls]
        self.assertEqual(requested_pages, [1, 2, 3, 3, 3])

    def test_arbeitnow_matches_full_page_then_uses_metadata_pagination(self):
        first_page_jobs = [
            arbeitnow_job(f"irrelevant-{index}", "Senior Accountant") for index in range(45)
        ]
        first_page_jobs.append(arbeitnow_job("first-match", "Junior Machine Learning Engineer"))
        client = FakeClient(
            [
                FakeResponse(
                    200,
                    payload={
                        "data": first_page_jobs,
                        "meta": {"current_page": 1, "last_page": 2},
                    },
                ),
                FakeResponse(
                    200,
                    payload={
                        "data": [arbeitnow_job("second-match", "Machine Learning Intern")],
                        "meta": {"current_page": 2, "last_page": 2},
                    },
                ),
            ]
        )
        with patch("scrapers.arbeitnow.httpx.AsyncClient", return_value=client):
            jobs = asyncio.run(
                ArbeitnowScraper().fetch("junior machine learning", limit=2, max_pages=2)
            )

        self.assertEqual([job.job_id for job in jobs], ["first-match", "second-match"])
        self.assertEqual(client.calls[1]["kwargs"]["params"]["page"], 2)

    def test_landing_jobs_reads_current_json_search_schema(self):
        client = FakeClient(
            [
                FakeResponse(
                    200,
                    payload={
                        "offers": [
                            {
                                "id": 42,
                                "title": "Junior Python Developer",
                                "company_name": "Acme",
                                "location": "Portugal",
                                "url": "https://landing.jobs/at/acme/junior-python",
                                "remote": True,
                                "experience_level": "Junior",
                                "skills": [{"name": "Python"}],
                            }
                        ],
                        "last_page?": True,
                    },
                )
            ]
        )
        with patch("scrapers.landing_jobs.httpx.AsyncClient", return_value=client):
            jobs = asyncio.run(LandingJobsScraper().fetch("python", limit=10))

        self.assertEqual([job.job_id for job in jobs], ["42"])
        self.assertEqual(jobs[0].seniority, "Junior")
        self.assertEqual(jobs[0].tags, ["Python"])

    def test_arbeitnow_query_tokens_do_not_match_inside_other_words(self):
        client = FakeClient(
            [
                FakeResponse(
                    200,
                    payload={
                        "data": [arbeitnow_job("international", "International Sales Manager")],
                        "meta": {"current_page": 1, "last_page": 1},
                    },
                )
            ]
        )
        with patch("scrapers.arbeitnow.httpx.AsyncClient", return_value=client):
            jobs = asyncio.run(ArbeitnowScraper().fetch("intern", limit=10))
        self.assertEqual(jobs, [])

    def test_remoteok_broad_fetch_omits_tag_parameter(self):
        client = FakeClient(
            [
                FakeResponse(
                    200,
                    payload=[
                        {"legal": "metadata"},
                        {
                            "id": "1",
                            "position": "Junior Software Engineer",
                            "company": "Acme",
                            "url": "https://remoteok.com/jobs/1",
                        },
                    ],
                )
            ]
        )
        with patch("scrapers.remoteok.httpx.AsyncClient", return_value=client):
            jobs = asyncio.run(RemoteOKScraper().fetch(None, limit=10))
        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertEqual(client.calls[0]["kwargs"]["params"], {})


if __name__ == "__main__":
    unittest.main()
