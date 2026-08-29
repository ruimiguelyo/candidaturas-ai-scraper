import asyncio
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from company_ranker import CompanyRanker
from exporter import export_public_jobs
from filter_engine import JobFilterEngine
from hiring_intelligence import HiringIntelligence, get_max_hiring_lookups
from main import AIJobPipeline, select_new_jobs
from models import JobPost


def make_job(title="Junior AI Engineer", company="Example", **kwargs):
    return JobPost(
        source=kwargs.pop("source", "Test"),
        job_id=kwargs.pop("job_id", "1"),
        title=title,
        company=company,
        location=kwargs.pop("location", "Portugal"),
        job_url=kwargs.pop("job_url", "https://jobs.example/1"),
        **kwargs,
    )


class TestSafetyAndExports(unittest.TestCase):
    def test_non_technical_and_mixed_senior_ai_titles_are_rejected(self):
        rejected = [
            "Marketing AI Intern",
            "Junior Account Executive - AI Sales",
            "Summer Business Analyst Intern - Tech & AI",
            "AI & Operations Intern",
            "Legal Analyst Intern — AI & Competition Law",
            "AI Agent Developer (Júnior / Sénior)",
        ]
        for title in rejected:
            self.assertIsNone(JobFilterEngine.pre_filter_job(make_job(title)), title)

    def test_location_compatibility_is_explicit_not_assumed(self):
        portugal = make_job(location="Lisbon, Portugal")
        us_only = make_job(location="United States", is_remote=True)
        unknown_remote = make_job(location="Remote", is_remote=True)

        JobFilterEngine.pre_filter_job(portugal)
        JobFilterEngine.pre_filter_job(us_only)
        JobFilterEngine.pre_filter_job(unknown_remote)

        self.assertEqual(portugal.location_compatibility, "confirmed")
        self.assertEqual(us_only.location_compatibility, "unlikely")
        self.assertEqual(unknown_remote.location_compatibility, "conditional")
        self.assertTrue(us_only.location_notes)
        with patch.dict(os.environ, {"INCLUDE_UNLIKELY_LOCATIONS": "false"}):
            self.assertFalse(JobFilterEngine.is_eligible_after_rating(us_only))
        with patch.dict(os.environ, {"INCLUDE_UNLIKELY_LOCATIONS": "true"}):
            self.assertTrue(JobFilterEngine.is_eligible_after_rating(us_only))

    def test_linkedin_parser_rejects_non_profile_urls(self):
        result = {
            "title": "Alex Example - Head of AI - Acme | LinkedIn",
            "link": "https://attacker.example/alex",
            "snippet": "Head of AI at Acme",
        }
        self.assertIsNone(HiringIntelligence._parse_linkedin_result(result, "Acme"))

    def test_search_result_for_another_employer_is_rejected(self):
        job = make_job(company="Euronext")
        unrelated = {
            "title": "Leam Delaney - Engineering Manager @ Akuity | LinkedIn",
            "link": "https://www.linkedin.com/in/leam-delaney",
            "snippet": "Engineering Manager at Akuity building platform teams.",
        }
        with patch.object(HiringIntelligence, "search_public_web", return_value=[unrelated]):
            outreach = asyncio.run(HiringIntelligence.enrich_single_job(job, {"projects": [], "skills": []}))
        self.assertFalse(outreach["target_found"])

    def test_glassdoor_rating_requires_company_match(self):
        self.assertTrue(
            CompanyRanker._glassdoor_result_matches_company(
                "Acme",
                "Acme has a rating of 4.2 out of 5",
                "https://www.glassdoor.com/Overview/Acme-Overview-E1.htm",
            )
        )
        self.assertFalse(
            CompanyRanker._glassdoor_result_matches_company(
                "Euronext",
                "Akuity has a rating of 4.8 out of 5",
                "https://www.glassdoor.com/Overview/Akuity-Overview-E1.htm",
            )
        )

    def test_public_exports_remove_private_fields_and_escape_csv_formulas(self):
        job = make_job(
            title="=HYPERLINK(\"https://attacker.example\",\"click\")",
            description_snippet="<p>private third-party body</p>",
            human_outreach={"target_found": True, "name": "Private Person"},
        )
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "jobs.csv"
            json_path = Path(directory) / "jobs.json"
            rows = export_public_jobs([job], csv_path, json_path)

            self.assertNotIn("human_outreach", rows[0])
            self.assertNotIn("description_snippet", rows[0])
            saved_json = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertNotIn("Private Person", json.dumps(saved_json))
            with csv_path.open(encoding="utf-8", newline="") as csv_file:
                saved_csv = next(csv.DictReader(csv_file))
            self.assertTrue(saved_csv["title"].startswith("'="))

    def test_daily_digest_selects_only_jobs_absent_from_previous_snapshot(self):
        existing = make_job(job_id="existing", job_url="https://jobs.example/existing")
        new = make_job(job_id="new", job_url="https://jobs.example/new")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "previous.json"
            snapshot.write_text(
                json.dumps([existing.model_dump(exclude={"human_outreach", "description_snippet"})]),
                encoding="utf-8",
            )
            selected = select_new_jobs([existing, new], snapshot)
        self.assertEqual([job.job_id for job in selected], ["new"])

    def test_hiring_lookup_limit_is_bounded(self):
        with patch.dict(os.environ, {"MAX_HIRING_LOOKUPS": "-5"}):
            self.assertEqual(get_max_hiring_lookups(), 0)
        with patch.dict(os.environ, {"MAX_HIRING_LOOKUPS": "500"}):
            self.assertEqual(get_max_hiring_lookups(), 50)

    def test_all_source_failures_stop_before_export(self):
        pipeline = AIJobPipeline()
        for scraper_name in (
            "linkedin",
            "himalayas",
            "arbeitnow",
            "remoteok",
            "itjobs",
            "jobicy",
            "landing_jobs",
        ):
            getattr(pipeline, scraper_name).fetch = AsyncMock(side_effect=RuntimeError("offline"))

        with self.assertRaisesRegex(RuntimeError, "Todas as fontes falharam"):
            asyncio.run(pipeline.run())

    def test_partial_source_failure_preserves_healthy_results(self):
        pipeline = AIJobPipeline()
        healthy_job = make_job()
        calls = 0

        async def one_success(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [healthy_job]
            if calls <= 10:
                return []
            raise RuntimeError("offline")

        pipeline.linkedin.fetch = one_success
        himalayas_calls = 0

        async def second_healthy_source(*args, **kwargs):
            nonlocal himalayas_calls
            himalayas_calls += 1
            if himalayas_calls == 1:
                return []
            raise RuntimeError("offline")

        pipeline.himalayas.fetch = second_healthy_source
        for scraper_name in (
            "arbeitnow",
            "remoteok",
            "itjobs",
            "jobicy",
            "landing_jobs",
        ):
            getattr(pipeline, scraper_name).fetch = AsyncMock(side_effect=RuntimeError("offline"))

        with patch.object(CompanyRanker, "enrich_jobs_async", new=AsyncMock(return_value=None)):
            with patch.object(HiringIntelligence, "enrich_jobs_async", new=AsyncMock(return_value=None)):
                jobs = asyncio.run(pipeline.run())
        self.assertEqual([job.title for job in jobs], [healthy_job.title])


if __name__ == "__main__":
    unittest.main()
