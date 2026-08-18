import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from company_ranker import CompanyRanker
from hiring_intelligence import HiringIntelligence, empty_outreach
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
        location=kwargs.pop("location", "Portugal"),
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

    def test_teamlyzer_rejects_partial_name_matches(self):
        self.assertEqual(
            CompanyRanker._company_similarity("Cambium Learning Group", "Further Learning Group"),
            0.0,
        )
        self.assertEqual(
            CompanyRanker._company_similarity(
                "Analytical Mechanics Associates, Inc.", "Alexandre Law Firm Associates"
            ),
            0.0,
        )

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

    def test_email_keeps_location_sections(self):
        data = [
            make_job("Remote AI Intern", "Remote Co", location="Worldwide", modality="100% Remote").model_dump(),
            make_job("Hybrid AI Intern", "Lisbon Co", location="Lisbon", modality="Hybrid").model_dump(),
            make_job("Other AI Intern", "Porto Co", location="Porto", modality="On-site").model_dump(),
        ]
        email = generate_html_email(data)
        self.assertIn("100% Remoto", email)
        self.assertIn("Lisboa &amp; Região", email)
        self.assertIn("Outras Localizações", email)

    def test_deduplication_keeps_distinct_urls(self):
        first = make_job("Junior AI Engineer", job_id="1", job_url="https://jobs.example/a")
        second = make_job("Junior AI Engineer", job_id="2", job_url="https://jobs.example/b")
        self.assertNotEqual(first.deduplication_key(), second.deduplication_key())

    def test_hiring_intelligence_disabled_flag(self):
        """Testa se com HIRING_INTELLIGENCE_ENABLED=false o pipeline nao altera nada."""
        jobs = [make_job("Junior AI Engineer", "Acme")]
        with patch.dict(os.environ, {"HIRING_INTELLIGENCE_ENABLED": "false"}):
            asyncio.run(HiringIntelligence.enrich_jobs_async(jobs))
        self.assertIsNone(jobs[0].human_outreach)

    def test_hiring_intelligence_no_results_returns_empty_outreach(self):
        """Testa se a ausencia de resultados devolve o schema correto de target nao encontrado."""
        job = make_job("Junior AI Engineer", "Unknown Company")
        profile = {"name": "Rui", "projects": [], "skills": ["Python"]}
        with patch.object(HiringIntelligence, "search_public_web", return_value=[]):
            outreach = asyncio.run(HiringIntelligence.enrich_single_job(job, profile))
        self.assertFalse(outreach["target_found"])
        self.assertEqual(outreach["target_type"], "NONE")
        self.assertEqual(outreach["confidence"], "NONE")
        self.assertEqual(outreach["outreach_recommendation"], "NO")

    def test_hiring_intelligence_high_confidence_hiring_manager(self):
        """Testa se um Engineering Manager na area e localizacao corretas recebe HIGH confidence e YES."""
        job = make_job("Junior AI Engineer", "TechCorp", location="Lisbon, Portugal")
        profile = {
            "name": "Rui Miguel",
            "projects": [{"name": "RAG & Semantic Search Pipeline", "description": "built RAG"}],
            "skills": ["Python", "PyTorch", "FastAPI"]
        }
        fake_search_results = [{
            "title": "Alexandre Santos - Head of AI & Machine Learning - TechCorp | LinkedIn",
            "link": "https://pt.linkedin.com/in/alexandre-santos-tech",
            "snippet": "Leading AI and Machine Learning Engineering team at TechCorp Lisbon. We are hiring engineers."
        }]
        with patch.object(HiringIntelligence, "search_public_web", return_value=fake_search_results):
            outreach = asyncio.run(HiringIntelligence.enrich_single_job(job, profile))
        self.assertTrue(outreach["target_found"])
        self.assertEqual(outreach["target_type"], "HIRING_MANAGER")
        self.assertEqual(outreach["name"], "Alexandre Santos")
        self.assertEqual(outreach["confidence"], "HIGH")
        self.assertEqual(outreach["outreach_recommendation"], "YES")
        self.assertIsNotNone(outreach["suggested_message"])
        self.assertIn("Alexandre", outreach["suggested_message"])

    def test_hiring_intelligence_recruiter_fallback(self):
        """Testa se quando nao ha hiring manager, encontra um recruiter tecnico com fallback."""
        job = make_job("Junior AI Engineer", "TechCorp", location="Portugal")
        profile = {"name": "Rui", "projects": [], "skills": ["Python"]}

        def fake_search(query, max_results=3):
            if "Technical Recruiter" in query:
                return [{
                    "title": "Mariana Costa - Technical Recruiter - TechCorp | LinkedIn",
                    "link": "https://pt.linkedin.com/in/mariana-costa-recruiter",
                    "snippet": "Technical Recruiter at TechCorp Portugal hiring AI and Software Engineers."
                }]
            return []

        with patch.object(HiringIntelligence, "search_public_web", side_effect=fake_search):
            outreach = asyncio.run(HiringIntelligence.enrich_single_job(job, profile))
        self.assertTrue(outreach["target_found"])
        self.assertEqual(outreach["target_type"], "RECRUITER")
        self.assertEqual(outreach["name"], "Mariana Costa")
        self.assertIn(outreach["confidence"], ("HIGH", "MEDIUM"))

    def test_hiring_intelligence_max_lookups_and_order_preservation(self):
        """Testa se MAX_HIRING_LOOKUPS enriquece apenas as primeiras N vagas sem alterar a ordem."""
        jobs = [make_job(f"Job {i}", f"Company {i}", rating_score=float(10 - i)) for i in range(8)]
        original_order = [j.title for j in jobs]

        with patch.dict(os.environ, {"MAX_HIRING_LOOKUPS": "3"}):
            with patch.object(HiringIntelligence, "enrich_single_job", return_value={"target_found": False}):
                asyncio.run(HiringIntelligence.enrich_jobs_async(jobs))

        self.assertEqual([j.title for j in jobs], original_order, "A ordem das vagas NUNCA deve ser alterada!")
        self.assertIsNotNone(jobs[0].human_outreach)
        self.assertIsNotNone(jobs[1].human_outreach)
        self.assertIsNotNone(jobs[2].human_outreach)
        self.assertIsNone(jobs[3].human_outreach)
        self.assertIsNone(jobs[4].human_outreach)

    def test_hiring_intelligence_fail_open_on_exception(self):
        """Testa se qualquer excecao de rede/parser e tratada silenciosamente sem abortar."""
        jobs = [make_job("Junior AI Engineer", "Acme")]
        with patch.object(HiringIntelligence, "enrich_single_job", side_effect=RuntimeError("Network failure")):
            asyncio.run(HiringIntelligence.enrich_jobs_async(jobs))
        self.assertIsNotNone(jobs[0].human_outreach)
        self.assertFalse(jobs[0].human_outreach["target_found"])

    def test_email_renders_outreach_block_when_present(self):
        """Testa se o template de email renderiza o bloco estatico de outreach quando encontrado."""
        job = make_job("Junior AI Engineer", "InnoWave", rating_score=4.2).model_dump()
        job["human_outreach"] = {
            "target_found": True,
            "target_type": "HIRING_MANAGER",
            "name": "Alexandre Santos",
            "current_title": "Head of AI",
            "profile_url": "https://linkedin.com/in/alexandre-santos",
            "confidence": "HIGH",
            "outreach_recommendation": "YES",
            "evidence": ["Lidera a equipa de AI na InnoWave."],
            "personalization_hook": "A equipa trabalha em RAG.",
            "suggested_message": "Hi Alexandre — I applied today for the Junior AI Engineer position."
        }
        email = generate_html_email([job])
        self.assertIn("Alexandre Santos", email)
        self.assertIn("LIKELY HIRING TARGET", email)
        self.assertIn("Confidence: HIGH", email)
        self.assertIn("Suggested LinkedIn Message", email)


if __name__ == "__main__":
    unittest.main()
