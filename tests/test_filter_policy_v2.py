import math
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filter_engine import FilterDecision, JobFilterEngine
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


class TestFilterPolicyV2(unittest.TestCase):
    def assert_rejected(self, job, reason_code):
        decision = JobFilterEngine.evaluate_job(job)
        self.assertIsInstance(decision, FilterDecision)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason_code, reason_code)
        self.assertTrue(decision.reason_detail.strip())
        self.assertIsNone(decision.job)
        self.assertIsNone(decision.domain_type)
        return decision

    def assert_accepted(self, job, domain_type=None):
        decision = JobFilterEngine.evaluate_job(job)
        self.assertIsInstance(decision, FilterDecision)
        self.assertTrue(decision.accepted, decision.reason_detail)
        self.assertEqual(decision.reason_code, "accepted")
        self.assertTrue(decision.reason_detail.strip())
        self.assertIs(decision.job, job)
        if domain_type is not None:
            self.assertEqual(decision.domain_type, domain_type)
        return decision

    def test_deloitte_is_always_excluded(self):
        jobs = [
            make_job("Junior AI Engineer", company="Deloitte"),
            make_job("Deloitte New Grad Python Developer", company="Another Company"),
            make_job(
                "Python Developer",
                company="Deloitte Digital Portugal",
                seniority="Entry Level",
                description_snippet="No experience required.",
            ),
        ]
        for job in jobs:
            with self.subTest(title=job.title, company=job.company):
                self.assert_rejected(job, "excluded_company")

    def test_recent_entry_level_search_can_supply_missing_title_signal(self):
        decision = self.assert_accepted(
            make_job(
                "Software Engineer",
                discovery_query="Entry Level Software Engineer",
            ),
            "SWE",
        )
        self.assertIn("discovery_query", decision.reason_detail)

    def test_bare_mid_title_is_rejected_even_from_entry_level_search(self):
        self.assert_rejected(
            make_job(
                "Mid Data Analytics Engineer",
                discovery_query="Junior Data Engineer",
            ),
            "excluded_seniority",
        )

    def test_clearly_senior_titles_are_rejected_before_entry_evidence(self):
        titles = [
            "Senior AI Engineer",
            "Sénior Python Developer",
            "Sr. Frontend Engineer",
            "Lead Mobile Engineer",
            "Principal Security Engineer",
            "Staff Platform Engineer",
            "Mid-Level Android Developer",
            "Engenheiro Python Pleno",
            "Software Engineer JR/PL",
            "Jr/Mid Data Engineer",
            "Director of Artificial Intelligence",
            "Head of Machine Learning",
            "Junior AI Engineer / Senior AI Engineer",
            "Field Technician III",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assert_rejected(
                    make_job(title, seniority="Entry Level", description_snippet="0-2 years"),
                    "excluded_seniority",
                )

    def test_clearly_senior_metadata_is_also_rejected(self):
        for seniority in ("Senior", "Midweight"):
            with self.subTest(seniority=seniority):
                self.assert_rejected(
                    make_job(
                        "Python Developer",
                        seniority=seniority,
                        description_snippet="No prior experience required.",
                    ),
                    "excluded_seniority",
                )

    def test_specific_non_technical_functions_remain_excluded(self):
        titles = [
            "Junior Account Executive - AI Sales",
            "Business Analyst Intern - Tech & AI",
            "AI Marketing Trainee",
            "Legal AI Intern",
            "AI Content Writer Intern",
            "Learning and Development Manager - Applied AI",
            "HR Recruitment Intern at an AI company",
            "Business Developer Junior - Software Market",
            "Junior Product Manager, AI Platform",
            "Business Operations Intern - AI",
            "AI & Operations Intern",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assert_rejected(make_job(title), "excluded_non_technical")

    def test_ambiguous_operations_content_manager_and_gerente_are_not_generic_blocks(self):
        cases = [
            ("Machine Learning Operations Intern", "AI"),
            ("Content Platform Engineer Intern", "SWE"),
            ("AI Engineering Manager Associate", "AI"),
            ("Gerente de Engenharia Python — New Grad", "SWE"),
        ]
        for title, domain_type in cases:
            with self.subTest(title=title):
                self.assert_accepted(make_job(title), domain_type)

    def test_all_new_entry_level_signals_are_accepted_from_seniority(self):
        signals = [
            "Associate",
            "New Grad",
            "New Graduate",
            "Apprentice",
            "Apprenticeship",
            "Academy",
            "Level I",
            "Level 1",
            "0-2 years",
            "0–2 years",
            "0 to 2 years",
            "No experience",
            "No prior experience",
        ]
        for signal in signals:
            with self.subTest(signal=signal):
                job = make_job("Python Developer", seniority=signal)
                decision = self.assert_accepted(job, "SWE")
                self.assertIn("seniority", decision.reason_detail)

    def test_entry_level_can_be_demonstrated_in_description(self):
        descriptions = [
            "This role is open to applicants with 0-2 years of experience.",
            "No experience required; training is provided.",
            "Join our engineering academy and learn on the job.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                decision = self.assert_accepted(
                    make_job("Python Developer", description_snippet=description),
                    "SWE",
                )
                self.assertIn("description_snippet", decision.reason_detail)

    def test_associate_degree_in_description_is_not_entry_level_evidence(self):
        self.assert_rejected(
            make_job(
                "Software Engineer",
                description_snippet="An associate degree or certification is required.",
            ),
            "missing_entry_level_signal",
        )

    def test_expanded_technical_domains_are_supported(self):
        titles = [
            "Frontend Developer Intern",
            "Mobile Developer Intern",
            "iOS Developer Intern",
            "Android Developer Intern",
            "Cybersecurity Analyst Intern",
            "Security Engineer Intern",
            "Python Developer Intern",
            "Platform Intern",
            "Platform Engineering Intern",
            "SRE Intern",
            "Site Reliability Engineer Intern",
            "Embedded Systems Developer Intern",
            "Product Engineering Intern",
            "Web Developer Intern",
            "Application Developer Intern",
            "Test Engineer Intern",
            "Automation Engineering Intern",
            "QA Automation Intern",
        ]
        for title in titles:
            with self.subTest(title=title):
                decision = self.assert_accepted(make_job(title), "SWE")
                self.assertEqual(decision.job.category, "Top-Tier Software Engineering")

    def test_portuguese_technical_domains_are_supported(self):
        titles = [
            "Programador Júnior",
            "Estágio em Cibersegurança",
            "Engenheira de Segurança Informática Trainee",
            "Recém-licenciado em Desenvolvimento de Aplicações",
            "Bolsa de Investigação em Automação de Testes",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assert_accepted(make_job(title), "SWE")

    def test_audited_technical_titles_are_no_longer_false_negatives(self):
        titles = [
            "Analista de Dados e Investigação Operacional - Estágio",
            "Analista de Sistemas Júnior",
            "C++ Developer Trainee",
            "Curricular Internship - Testing",
            "Data Governance Junior Consultant",
            "Engenheiro de rede e sistemas - Trainee",
            "Engineer I, Software Quality",
            "Entry Level QA",
            "Estágio Application Support",
            "Graduate Cyber Risk Analyst",
            "IAM Analyst Trainee",
            "Information Technology Intern",
            "Junior RPA Developer",
            "Junior SAP Integration Developer",
            "Quality Assurance Engineering Intern",
            "Research Engineer Intern",
            "Technology Graduate Programme",
            "Computer Sciences Intern",
            "Data and Reporting Analyst Trainee",
            "Fuel Data Management - Curricular Internship",
            "Junior Data & Insights Specialist",
            "Political Data Intern",
            "Quality Engineer - Trainee",
            "Systems Operations Intern",
            "Weather Analytics Internship Program",
            "Zero-Emission Data & Research Intern",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assert_accepted(make_job(title), None)

    def test_communications_systems_engineer_is_not_mistaken_for_comms(self):
        self.assert_accepted(make_job("Communications Systems Engineer Intern"), "SWE")

    def test_existing_seniority_metadata_is_preserved_on_acceptance(self):
        job = make_job(
            "Python Developer Intern",
            seniority="Entry Level - source metadata",
        )
        self.assert_accepted(job, "SWE")
        self.assertEqual(job.seniority, "Entry Level - source metadata")

    def test_supported_domain_can_still_come_from_tags(self):
        decision = self.assert_accepted(
            make_job("Engineering Associate", tags=["application security"]),
            "SWE",
        )
        self.assertEqual(decision.job.category, "Top-Tier Software Engineering")

    def test_missing_entry_level_and_unsupported_domain_have_distinct_reasons(self):
        self.assert_rejected(make_job("Python Developer"), "missing_entry_level_signal")
        self.assert_rejected(make_job("Junior Accountant"), "unsupported_domain")

    def test_location_classification_never_eliminates_a_job(self):
        job = make_job(
            "Python Developer Intern",
            location="United States",
            is_remote=False,
            rating_score=0.0,
        )
        self.assert_accepted(job, "SWE")
        self.assertEqual(job.location_compatibility, "unlikely")
        self.assertTrue(JobFilterEngine.is_eligible_after_rating(job))

    def test_rating_never_eliminates_a_job(self):
        job = make_job("Software Engineer Intern")
        for rating in (0.0, 2.9, 3.0, 3.1, math.nan, None, "invalid"):
            with self.subTest(rating=rating):
                job.rating_score = rating
                self.assertTrue(JobFilterEngine.is_eligible_after_rating(job))

    def test_pre_filter_job_remains_backwards_compatible(self):
        accepted = make_job("Frontend Developer", seniority="Associate")
        legacy_result = JobFilterEngine.pre_filter_job(accepted)
        self.assertIsNotNone(legacy_result)
        self.assertIs(legacy_result[0], accepted)
        self.assertEqual(legacy_result[1], "SWE")

        rejected = make_job("Python Developer")
        self.assertIsNone(JobFilterEngine.pre_filter_job(rejected))


if __name__ == "__main__":
    unittest.main()
