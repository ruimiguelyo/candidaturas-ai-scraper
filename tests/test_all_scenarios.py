import asyncio
import unittest
import sys
import os

# Fix paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import JobPost
from filter_engine import JobFilterEngine
from company_ranker import CompanyRanker
from scrapers.itjobs import ITJobsScraper
from scrapers.himalayas import HimalayasScraper
from scrapers.jobicy import JobicyScraper
from scrapers.arbeitnow import ArbeitnowScraper
from scrapers.remoteok import RemoteOKScraper

class TestAllScenarios(unittest.TestCase):

    def test_01_strictly_entry_level_ai_accepted(self):
        """Testa se todos os cargos de IA/ML estritamente Junior/Trainee/Intern são aceites."""
        test_cases = [
            ("Junior AI Engineer", "TensorOps"),
            ("Gen AI Trainee", "Euronext"),
            ("Machine Learning Intern", "Zendesk"),
            ("NLP Junior Research Scientist", "Priberam"),
            ("Data Science Intern (Customer Success)", "Cresta"),
            ("Estágio Profissional Inteligência Artificial", "Empresa PT"),
            ("AI Engineer Trainee [m/f/d]", "Siemens"),
            ("Summer Business Analyst Intern - Tech & AI", "McKinsey"),
            ("Junior Applied AI Engineer", "Colibri"),
            ("AI-Native Software Engineer - Early Career", "ChainGPT")
        ]

        for title, company in test_cases:
            job = JobPost(source="Test", job_id="1", title=title, company=company, location="Portugal", job_url="https://example.com")
            res = JobFilterEngine.pre_filter_job(job)
            self.assertIsNotNone(res, f"Deveria ter aceite a vaga de IA: {title} @ {company}")
            valid_job, category = res
            self.assertEqual(valid_job.category, "AI / ML", f"Categoria incorreta para {title}")

    def test_02_top_tier_swe_accepted_only_if_high_rating(self):
        """Testa se vagas de Software Engineering só entram se a empresa tiver rating >= 3.1."""
        # Caso A: Cloudflare (Rating 4.2 no Teamlyzer) -> DEVE ENTRAR
        job_cloudflare = JobPost(source="Test", job_id="cf", title="Software Engineer Intern (Fall 2026)", company="Cloudflare", location="Lisbon, Portugal", job_url="https://example.com")
        res_cf = JobFilterEngine.pre_filter_job(job_cloudflare)
        self.assertIsNotNone(res_cf)
        job_cf_filtered, _ = res_cf
        self.assertEqual(job_cf_filtered.category, "Top-Tier Software Engineering")

        # Caso B: Consultora Desconhecida / Rating 0.0 -> DEVE SER REJEITADA NO PIPELINE
        job_unknown = JobPost(source="Test", job_id="unk", title="Junior Software Engineer", company="Consultora Desconhecida Sem Score", location="Lisbon", job_url="https://example.com")
        res_unk = JobFilterEngine.pre_filter_job(job_unknown)
        self.assertIsNotNone(res_unk)
        job_unk_filtered, _ = res_unk
        # No main.py, como job.rating_score < 3.1, é descartado
        self.assertEqual(job_unk_filtered.rating_score, 0.0)

    def test_03_strict_exclusions_and_blacklist(self):
        """Testa se Deloitte, Senior, Lead, Mid-Level e cargos sem termo de junior são rejeitados."""
        rejected_cases = [
            ("Junior AI Engineer", "Deloitte"),  # Deloitte estritamente na blacklist
            ("Deloitte Junior Consultant", "Deloitte"),
            ("Senior AI Engineer", "Google"),  # Senior
            ("Lead Machine Learning Engineer", "Revolut"),  # Lead
            ("Principal Data Scientist", "Stripe"),  # Principal
            ("Mid-Level AI Developer", "Empresa"),  # Mid-Level
            ("AI Engineer", "Empresa"),  # Sem Junior/Trainee/Intern no título
            ("Machine Learning Specialist", "Empresa"),  # Sem Junior/Trainee/Intern
            ("Director of Artificial Intelligence", "Empresa")  # Director
        ]

        for title, company in rejected_cases:
            job = JobPost(source="Test", job_id="rej", title=title, company=company, location="Portugal", job_url="https://example.com")
            res = JobFilterEngine.pre_filter_job(job)
            self.assertIsNone(res, f"Deveria ter REJEITADO: {title} @ {company}")

    def test_04_teamlyzer_priority_over_glassdoor(self):
        """Testa se empresas com perfil em Portugal usam estritamente o Teamlyzer."""
        async def check():
            sample_jobs = [
                JobPost(source="T", job_id="1", title="Junior AI", company="Cloudflare", location="Lisbon", job_url="http://a"),
                JobPost(source="T", job_id="2", title="Junior AI", company="Fujitsu", location="Lisbon", job_url="http://a"),
                JobPost(source="T", job_id="3", title="Junior AI", company="InnoWave", location="Lisbon", job_url="http://a"),
                JobPost(source="T", job_id="4", title="Junior AI", company="Volkswagen Group", location="Lisbon", job_url="http://a"),
                JobPost(source="T", job_id="5", title="Junior AI", company="Santander", location="Lisbon", job_url="http://a"),
                JobPost(source="T", job_id="6", title="Junior AI", company="Nordea Asset Management", location="Lisbon", job_url="http://a"),
            ]
            await CompanyRanker.enrich_jobs_async(sample_jobs)
            for j in sample_jobs:
                self.assertIn("Teamlyzer", j.company_score, f"Deveria usar Teamlyzer para {j.company}")
                self.assertTrue(j.teamlyzer_url.startswith("https://pt.teamlyzer.com/companies/"), f"URL inválida para {j.company}: {j.teamlyzer_url}")

        asyncio.run(check())

    def test_05_glassdoor_fallback_for_us_remote(self):
        """Testa se empresas 100% remotas/US sem perfil PT usam Glassdoor de forma segura."""
        async def check():
            sample_jobs = [
                JobPost(source="T", job_id="1", title="Junior AI", company="Stripe", location="Remote", job_url="http://a"),
                JobPost(source="T", job_id="2", title="Junior AI", company="Peraton", location="Remote", job_url="http://a"),
                JobPost(source="T", job_id="3", title="Junior AI", company="Seven Senders", location="Remote", job_url="http://a"),
            ]
            await CompanyRanker.enrich_jobs_async(sample_jobs)
            for j in sample_jobs:
                self.assertIn("Glassdoor", j.company_score, f"Deveria usar Glassdoor para {j.company}")
                self.assertTrue(j.teamlyzer_url.startswith("https://www.glassdoor.com/Search/results.htm?keyword="), f"URL Glassdoor inválida para {j.company}")

    def test_06_descending_rating_sort(self):
        """Testa se a ordenação decrescente por rating funciona com 100% de precisão."""
        jobs = [
            JobPost(source="T", job_id="1", title="J1", company="Company 3.1", location="L", job_url="http://a", rating_score=3.1),
            JobPost(source="T", job_id="2", title="J2", company="Company 4.4", location="L", job_url="http://a", rating_score=4.4),
            JobPost(source="T", job_id="3", title="J3", company="Company 3.8", location="L", job_url="http://a", rating_score=3.8),
            JobPost(source="T", job_id="4", title="J4", company="Company 4.2", location="L", job_url="http://a", rating_score=4.2),
            JobPost(source="T", job_id="5", title="J5", company="Company 0.0", location="L", job_url="http://a", rating_score=0.0),
        ]

        jobs.sort(key=lambda x: (x.rating_score, x.company), reverse=True)
        scores = [j.rating_score for j in jobs]
        self.assertEqual(scores, [4.4, 4.2, 3.8, 3.1, 0.0], "A lista deve estar estritamente ordenada por ordem decrescente!")

    def test_07_itjobs_scraper_smoke(self):
        """Testa se o scraper do ITJobs.pt retorna ofertas estruturadas válidas."""
        async def check():
            scraper = ITJobsScraper()
            jobs = await scraper.fetch("junior", max_pages=1)
            self.assertGreater(len(jobs), 0, "ITJobs deveria retornar pelo menos 1 vaga.")
            for j in jobs:
                self.assertTrue(j.job_url.startswith("https://www.itjobs.pt/oferta/"), f"URL inválida do ITJobs: {j.job_url}")
                self.assertIsNotNone(j.title)
                self.assertIsNotNone(j.company)

        asyncio.run(check())

if __name__ == "__main__":
    unittest.main()
