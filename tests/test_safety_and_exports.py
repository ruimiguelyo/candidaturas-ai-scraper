import asyncio
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
        post_date=kwargs.pop("post_date", datetime.now(timezone.utc).date().isoformat()),
        **kwargs,
    )


class TestSafetyAndExports(unittest.TestCase):
    def test_non_technical_and_mixed_senior_ai_titles_are_rejected(self):
        rejected = [
            "Marketing AI Intern",
            "Junior Account Executive - AI Sales",
            "Summer Business Analyst Intern - Tech & AI",
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
            "remotive",
        ):
            getattr(pipeline, scraper_name).fetch = AsyncMock(side_effect=RuntimeError("offline"))

        with self.assertRaisesRegex(RuntimeError, "Todas as fontes falharam"):
            asyncio.run(pipeline.run())

    def test_partial_source_failure_preserves_healthy_results(self):
        pipeline = AIJobPipeline()
        call_number = 0

        async def distinct_linkedin_result(*_args, **_kwargs):
            nonlocal call_number
            call_number += 1
            return [
                make_job(
                    job_id=f"linkedin-{call_number}",
                    job_url=f"https://jobs.example/linkedin-{call_number}",
                    source="LinkedIn",
                )
            ]

        healthy_job = make_job(
            job_id="himalayas",
            job_url="https://jobs.example/himalayas",
            source="Himalayas",
        )
        pipeline.linkedin.fetch = AsyncMock(side_effect=distinct_linkedin_result)
        pipeline.himalayas.fetch = AsyncMock(return_value=[healthy_job])
        for scraper_name in (
            "arbeitnow",
            "remoteok",
            "itjobs",
            "jobicy",
            "landing_jobs",
            "remotive",
        ):
            getattr(pipeline, scraper_name).fetch = AsyncMock(side_effect=RuntimeError("offline"))

        with patch.object(CompanyRanker, "enrich_jobs_async", new=AsyncMock(return_value=None)):
            with patch.object(HiringIntelligence, "enrich_jobs_async", new=AsyncMock(return_value=None)):
                jobs = asyncio.run(pipeline.run())
        self.assertGreaterEqual(len(jobs), 5)
        self.assertTrue(any(row["reason_code"] == "duplicate" for row in pipeline.rejections))

    def test_empty_http_successes_do_not_replace_a_valid_snapshot(self):
        pipeline = AIJobPipeline()
        for scraper_name in (
            "linkedin",
            "himalayas",
            "arbeitnow",
            "remoteok",
            "itjobs",
            "jobicy",
            "landing_jobs",
            "remotive",
        ):
            getattr(pipeline, scraper_name).fetch = AsyncMock(return_value=[])

        with self.assertRaisesRegex(RuntimeError, "limiar de saúde"):
            asyncio.run(pipeline.run())

    def test_unique_filter_audits_one_duplicate_group_not_every_occurrence(self):
        pipeline = AIJobPipeline()
        repeated = [make_job(source=f"Search {index}") for index in range(8)]

        candidates = pipeline._filter_unique_jobs(repeated)

        self.assertEqual(len(candidates), 1)
        duplicate_rows = [
            row for row in pipeline.rejections if row["reason_code"] == "duplicate"
        ]
        self.assertEqual(len(duplicate_rows), 1)
        self.assertIn("8 ocorrências", duplicate_rows[0]["reason_detail"])

    def test_duplicate_variants_use_all_evidence_but_deloitte_always_wins(self):
        pipeline = AIJobPipeline()
        incomplete = make_job(title="Python Developer")
        richer = make_job(
            title="Python Developer",
            seniority="Entry Level",
            description_snippet="No experience required.",
        )
        self.assertEqual(len(pipeline._filter_unique_jobs([incomplete, richer])), 1)

        pipeline.rejections = []
        deloitte_copy = make_job(company="Deloitte")
        accepted_copy = make_job(company="Another Company")
        self.assertEqual(pipeline._filter_unique_jobs([accepted_copy, deloitte_copy]), [])
        self.assertEqual(
            [row["reason_code"] for row in pipeline.rejections],
            ["excluded_company"],
        )

    def test_export_and_display_publishes_jobs_and_audit_as_one_bundle(self):
        pipeline = AIJobPipeline()
        pipeline.rejections = [
            pipeline._rejection_record(
                make_job(title="Senior AI Engineer"),
                "semantic_filter",
                "excluded_seniority",
                "Senior role.",
            )
        ]

        with tempfile.TemporaryDirectory() as directory:
            pipeline.export_and_display([make_job()], output_dir=directory)
            output = Path(directory)
            jobs_json = output / "vagas_estritamente_junior_trainee_internship.json"
            jobs_csv = output / "vagas_estritamente_junior_trainee_internship.csv"
            audit_csv = output / "vagas_rejeitadas.csv"

            self.assertEqual(len(json.loads(jobs_json.read_text(encoding="utf-8"))), 1)
            with jobs_csv.open(encoding="utf-8", newline="") as csv_file:
                self.assertEqual(len(list(csv.DictReader(csv_file))), 1)
            with audit_csv.open(encoding="utf-8", newline="") as csv_file:
                audit_rows = list(csv.DictReader(csv_file))
            self.assertEqual([row["reason_code"] for row in audit_rows], ["excluded_seniority"])

    def test_export_and_display_writes_audit_even_with_zero_accepted_jobs(self):
        pipeline = AIJobPipeline()
        pipeline.rejections = [
            pipeline._rejection_record(
                make_job(company="Deloitte"),
                "semantic_filter",
                "excluded_company",
                "Explicit exclusion.",
            )
        ]

        with tempfile.TemporaryDirectory() as directory:
            pipeline.export_and_display([], output_dir=directory)
            output = Path(directory)
            self.assertEqual(
                json.loads(
                    (output / "vagas_estritamente_junior_trainee_internship.json").read_text(
                        encoding="utf-8"
                    )
                ),
                [],
            )
            with (output / "vagas_rejeitadas.csv").open(
                encoding="utf-8", newline=""
            ) as csv_file:
                self.assertEqual(len(list(csv.DictReader(csv_file))), 1)


if __name__ == "__main__":
    unittest.main()
