import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from company_ranker import CompanyRanker
from email_notifier import generate_html_email
from filter_engine import JobFilterEngine
from main import sort_jobs_by_rating
from models import JobPost


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None, url=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.url = url or "https://pt.teamlyzer.com/companies/example"

    def json(self):
        if self._payload is None:
            raise ValueError("no json fixture")
        return self._payload


TEAMLYZER_PROFILE = """
<html>
  <head>
    <link rel="canonical" href="https://pt.teamlyzer.com/companies/neotalent">
  </head>
  <body>
    <div class="company_header_basic_info"><h1 class="reduce-h1">Neotalent Conclusion</h1></div>
    <span itemprop="aggregateRating">
      <meta itemprop="ratingValue" content="2.5">
      <meta itemprop="reviewCount" content="22">
    </span>
  </body>
</html>
"""


class TeamlyzerFixtureClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if "/users/autocomplete_company/v2/" in url:
            return FakeResponse(payload={"results": [{"name": "Neotalent Conclusion", "slug": "neotalent"}]})
        if url.endswith("/companies/neotalent"):
            return FakeResponse(text=TEAMLYZER_PROFILE, url=url)
        return FakeResponse(status_code=404, url=url)


class EmptyTeamlyzerClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if "/users/autocomplete_company/v2/" in url:
            return FakeResponse(payload={"results": []}, url=url)
        return FakeResponse(status_code=404, url=url)


def make_job(title, company="Example", **kwargs):
    return JobPost(
        source="Test",
        job_id=kwargs.pop("job_id", title),
        title=title,
        company=company,
        location="Portugal",
        job_url=kwargs.pop("job_url", f"https://jobs.example/{title.replace(' ', '-') }"),
        **kwargs,
    )


class TestAllScenarios(unittest.TestCase):
    def setUp(self):
        self.original_cache = CompanyRanker._cache
        self.original_cache_loaded = CompanyRanker._cache_loaded
        CompanyRanker._cache = {}
        CompanyRanker._cache_loaded = True

    def tearDown(self):
        CompanyRanker._cache = self.original_cache
        CompanyRanker._cache_loaded = self.original_cache_loaded

    def test_strict_entry_level_ai_titles_are_accepted(self):
        titles = [
            "Junior AI Engineer",
            "Gen AI Trainee",
            "Machine Learning Intern",
            "NLP Junior Research Scientist",
            "Data Science Internship",
            "Estágio Profissional em Inteligência Artificial",
            "AI Engineer Trainee",
            "Graduate Data Scientist",
            "Early Career AI Engineer",
            "Bolsa de Investigação em Machine Learning",
        ]
        for title in titles:
            result = JobFilterEngine.pre_filter_job(make_job(title))
            self.assertIsNotNone(result, title)
            self.assertEqual(result[0].category, "AI / ML")

    def test_title_marker_is_mandatory_and_exclusions_always_win(self):
        rejected = [
            ("AI Engineer", "Example"),
            ("Research Scientist", "Example"),
            ("Professional AI Engineer", "Example"),
            ("Junior Program Manager, AI Studio", "Example"),
            ("Junior AI Engineer / Senior AI Engineer", "Example"),
            ("Junior AI Lead", "Example"),
            ("Junior Data Scientist", "Deloitte"),
            ("Junior Software Engineer (JR/PL)", "Example"),
            ("Mid-Level AI Developer", "Example"),
            ("Director of Artificial Intelligence", "Example"),
        ]
        for title, company in rejected:
            self.assertIsNone(JobFilterEngine.pre_filter_job(make_job(title, company)), title)

    def test_swe_description_cannot_bypass_rating_rule(self):
        job = make_job(
            "Software Engineer Intern",
            description_snippet="Work on a platform powered by artificial intelligence.",
            tags=["AI"],
        )
        result = JobFilterEngine.pre_filter_job(job)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].category, "Top-Tier Software Engineering")
        job.rating_score = 3.0
        self.assertFalse(JobFilterEngine.is_eligible_after_rating(job))
        job.rating_score = 3.1
        self.assertTrue(JobFilterEngine.is_eligible_after_rating(job))

    def test_ai_is_eligible_without_rating(self):
        job = make_job("Junior AI Engineer")
        result = JobFilterEngine.pre_filter_job(job)
        self.assertIsNotNone(result)
        self.assertTrue(JobFilterEngine.is_eligible_after_rating(result[0]))

    def test_dynamic_teamlyzer_resolves_compound_name(self):
        async def check():
            client = TeamlyzerFixtureClient()
            with patch.object(CompanyRanker, "save_cache"):
                score = await CompanyRanker.fetch_dynamic_teamlyzer("Neotalent Conclusion", client)
            self.assertEqual(score["platform"], "Teamlyzer")
            self.assertEqual(score["numeric"], 2.5)
            self.assertEqual(score["reviews"], "22 Reviews")
            self.assertEqual(score["url"], "https://pt.teamlyzer.com/companies/neotalent")
            self.assertTrue(any("autocomplete_company/v2" in call for call in client.calls))
            self.assertTrue(any(call.endswith("/companies/neotalent") for call in client.calls))

        asyncio.run(check())

    def test_stale_glassdoor_cache_is_revalidated_against_teamlyzer(self):
        async def check():
            CompanyRanker._cache["neotalentconclusion"] = {
                "platform": "Glassdoor",
                "score": "Ver Reviews",
                "numeric": 0.0,
                "reviews": "Pesquisa Global",
                "url": "https://www.glassdoor.com/Search/results.htm?keyword=Neotalent%20Conclusion",
            }
            client = TeamlyzerFixtureClient()
            with patch.object(CompanyRanker, "save_cache"):
                score = await CompanyRanker.get_score_async("Neotalent Conclusion", client)
            self.assertEqual(score["platform"], "Teamlyzer")
            self.assertEqual(CompanyRanker._cache["neotalentconclusion"]["url"], score["url"])

        asyncio.run(check())

    def test_glassdoor_is_fallback_only_after_teamlyzer_miss(self):
        async def check():
            client = EmptyTeamlyzerClient()
            glassdoor_html = """
            <div class="result">
              <a class="result__a" href="https://www.glassdoor.com/Overview/Acme-Overview-E1.htm">Acme</a>
              <a class="result__snippet">Company rating of 4.2 out of 5 stars from 100 company reviews</a>
            </div>
            """

            async def get_with_glassdoor(url, **kwargs):
                client.calls.append(url)
                if "duckduckgo.com" in url:
                    return FakeResponse(text=glassdoor_html, url=url)
                if "/users/autocomplete_company/v2/" in url:
                    return FakeResponse(payload={"results": []}, url=url)
                return FakeResponse(status_code=404, url=url)

            client.get = get_with_glassdoor
            with patch.object(CompanyRanker, "save_cache"):
                score = await CompanyRanker.get_score_async("Acme", client)
            self.assertEqual(score["platform"], "Glassdoor")
            self.assertEqual(score["numeric"], 4.2)
            self.assertTrue(score["url"].startswith("https://www.glassdoor.com/Search/results.htm?keyword="))
            teamlyzer_calls = [call for call in client.calls if "teamlyzer.com" in call]
            glassdoor_calls = [call for call in client.calls if "duckduckgo.com" in call]
            self.assertTrue(teamlyzer_calls)
            self.assertTrue(glassdoor_calls)

        asyncio.run(check())

    def test_teamlyzer_error_does_not_falsely_trigger_glassdoor(self):
        async def check():
            class OfflineClient:
                async def get(self, url, **kwargs):
                    raise httpx.ReadTimeout("offline")

            with patch.object(CompanyRanker, "fetch_dynamic_glassdoor", new_callable=AsyncMock) as glassdoor:
                score = await CompanyRanker.get_score_async("Unknown Company", OfflineClient())
            self.assertIsNone(score)
            glassdoor.assert_not_awaited()

        asyncio.run(check())

    def test_sorting_is_descending_and_email_preserves_it(self):
        jobs = [
            make_job("Junior AI 3.1", "Beta", rating_score=3.1),
            make_job("Junior AI 4.4", "Alpha", rating_score=4.4),
            make_job("Junior AI 0", "Omega", rating_score=0.0),
            make_job("Junior AI 4.2", "Gamma", rating_score=4.2),
        ]
        ordered = sort_jobs_by_rating(jobs)
        self.assertEqual([job.rating_score for job in ordered], [4.4, 4.2, 3.1, 0.0])

        data = [job.model_dump() for job in jobs]
        email = generate_html_email(data)
        self.assertLess(email.index("Junior AI 4.4"), email.index("Junior AI 4.2"))
        self.assertLess(email.index("Junior AI 4.2"), email.index("Junior AI 3.1"))
        self.assertLess(email.index("Junior AI 3.1"), email.index("Junior AI 0"))

    def test_deduplication_keeps_distinct_urls(self):
        first = make_job("Junior AI Engineer", job_id="1", job_url="https://jobs.example/a")
        second = make_job("Junior AI Engineer", job_id="2", job_url="https://jobs.example/b")
        self.assertNotEqual(first.deduplication_key(), second.deduplication_key())


if __name__ == "__main__":
    unittest.main()
