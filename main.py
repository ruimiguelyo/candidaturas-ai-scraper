import sys
import os

# Fix Windows console UTF-8 encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import asyncio
import logging
import csv
import json
from typing import List
import pandas as pd
from rich.console import Console
from rich.table import Table

from models import JobPost
from scrapers.linkedin import LinkedInScraper
from scrapers.himalayas import HimalayasScraper
from scrapers.arbeitnow import ArbeitnowScraper
from scrapers.remoteok import RemoteOKScraper
from scrapers.itjobs import ITJobsScraper
from scrapers.jobicy import JobicyScraper
from scrapers.landing_jobs import LandingJobsScraper
from filter_engine import JobFilterEngine
from company_ranker import CompanyRanker
from email_notifier import send_daily_email

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
console = Console(force_terminal=True, legacy_windows=False)

class AIJobPipeline:
    def __init__(self):
        self.linkedin = LinkedInScraper()
        self.himalayas = HimalayasScraper()
        self.arbeitnow = ArbeitnowScraper()
        self.remoteok = RemoteOKScraper()
        self.itjobs = ITJobsScraper()
        self.jobicy = JobicyScraper()
        self.landing_jobs = LandingJobsScraper()

    async def run(self) -> List[JobPost]:
        console.print("[bold cyan]>>> A recolher vagas Junior / Trainee / Internship (IA/ML + Software Engineering em Top Tech)...[/bold cyan]\n")

        tasks = [
            # 1. LinkedIn Portugal (IA/ML + Top Tech Internships)
            self.linkedin.fetch("Junior AI Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Machine Learning Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Internship", "Portugal", total_wanted=30),
            self.linkedin.fetch("Machine Learning Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Gen AI Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Data Scientist", "Portugal", total_wanted=30),
            self.linkedin.fetch("Data Science Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Software Engineer Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Software Engineering Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Systems Engineer Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Software Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior AI", "Lisbon, Portugal", total_wanted=30),
            self.linkedin.fetch("Software Intern", "Lisbon, Portugal", total_wanted=30),

            # 2. Landing.jobs (Portugal Tech Hub)
            self.landing_jobs.fetch("ai", limit=30),
            self.landing_jobs.fetch("machine learning", limit=30),
            self.landing_jobs.fetch("junior software", limit=30),
            self.landing_jobs.fetch("intern", limit=30),

            # 3. ITJobs Portugal
            self.itjobs.fetch("junior", limit=50),
            self.itjobs.fetch("estagio", limit=50),
            self.itjobs.fetch("ai", limit=50),

            # 4. Himalayas (Global Remote)
            self.himalayas.fetch("Junior AI", limit=40),
            self.himalayas.fetch("AI Intern", limit=40),
            self.himalayas.fetch("Machine Learning Intern", limit=40),
            self.himalayas.fetch("Software Engineer Intern", limit=40),
            self.himalayas.fetch("Junior Software Engineer", limit=40),
            self.himalayas.fetch("Junior Data", limit=40),

            # 5. Jobicy (Remote)
            self.jobicy.fetch("ai", count=30),
            self.jobicy.fetch("software engineer", count=30),
            self.jobicy.fetch("intern", count=30),

            # 6. Arbeitnow & RemoteOK
            self.arbeitnow.fetch("junior machine learning", limit=40),
            self.arbeitnow.fetch("software engineer intern", limit=40),
            self.remoteok.fetch("intern", limit=40),
            self.remoteok.fetch("junior", limit=40)
        ]

        raw_responses = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: List[JobPost] = []
        for res in raw_responses:
            if isinstance(res, list):
                all_jobs.extend(res)

        # Pré-filtro: Deduplicação e separação
        seen_keys = set()
        candidates: List[JobPost] = []

        for job in all_jobs:
            filter_res = JobFilterEngine.pre_filter_job(job)
            if not filter_res:
                continue

            valid_job, domain_type = filter_res
            key = valid_job.deduplication_key()
            if key not in seen_keys:
                seen_keys.add(key)
                candidates.append(valid_job)

        # Cruzamento assíncrono com Teamlyzer e Glassdoor
        console.print("[yellow]A cruzar empresas em paralelo com scores do Teamlyzer e Glassdoor...[/yellow]")
        await CompanyRanker.enrich_jobs_async(candidates)

        # Regra de Elegibilidade:
        # - Vagas de IA/ML: Entram sempre (desde que Junior/Trainee/Intern)
        # - Vagas de Software Engineering Geral: Entram APENAS se o rating da empresa for >= 3.1
        final_jobs: List[JobPost] = []
        for job in candidates:
            if job.category == "AI / ML":
                final_jobs.append(job)
            elif job.category == "Top-Tier Software Engineering":
                if job.rating_score >= 3.1:
                    final_jobs.append(job)
                else:
                    # Ignora vagas de SWE geral de empresas com rating < 3.1 ou desconhecidas
                    continue

        # ORDENAÇÃO: Da empresa com MAIOR rating (ex: 4.4, 4.2, 3.7) até à menor / sem rating
        final_jobs.sort(key=lambda x: (x.rating_score, x.company), reverse=True)

        console.print(f"[bold green]Total de vagas qualificadas (Ordenadas por Rating):[/bold green] {len(final_jobs)}\n")
        return final_jobs

    def export_and_display(self, jobs: List[JobPost]):
        if not jobs:
            console.print("[yellow]Nenhuma vaga passou pelo filtro nesta execução.[/yellow]")
            return

        table = Table(title="VAGAS QUALIFICADAS (ORDENADAS POR RATING DE EMPRESA)", show_lines=True)
        table.add_column("Score", style="bold yellow", width=16)
        table.add_column("Empresa", style="green", width=18)
        table.add_column("Título do Cargo", style="bold white", width=30)
        table.add_column("Categoria", style="cyan", width=12)
        table.add_column("Localização", style="magenta", width=18)
        table.add_column("Regime", style="yellow", width=10)
        table.add_column("Link de Candidatura", style="blue", width=36)

        data_rows = []
        for j in jobs:
            score_display = f"{j.company_score} ({j.company_reviews})" if j.company_score else "Sem rating"
            table.add_row(
                score_display,
                j.company,
                j.title,
                j.category,
                j.location,
                j.modality,
                j.job_url
            )
            data_rows.append(j.model_dump())

        console.print(table)

        # Exportar CSV e JSON
        csv_filename = "vagas_estritamente_junior_trainee_internship.csv"
        df = pd.DataFrame(data_rows)
        df.to_csv(csv_filename, index=False, quoting=csv.QUOTE_NONNUMERIC)

        json_filename = "vagas_estritamente_junior_trainee_internship.json"
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(data_rows, f, ensure_ascii=False, indent=2)

        console.print(f"\n[bold green]Ficheiro atualizado e ordenado por rating:[/bold green] {csv_filename}")
        console.print(f"[bold green]Ficheiro atualizado e ordenado por rating:[/bold green] {json_filename}")

        # Disparo de Email Notifier se configurado
        send_daily_email(json_filename, csv_filename)

if __name__ == "__main__":
    pipeline = AIJobPipeline()
    jobs = asyncio.run(pipeline.run())
    pipeline.export_and_display(jobs)
