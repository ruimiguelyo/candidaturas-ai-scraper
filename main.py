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
import math
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
from hiring_intelligence import HiringIntelligence
from email_notifier import send_daily_email

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
console = Console(force_terminal=True, legacy_windows=False)

OUTPUT_CSV = "vagas_estritamente_junior_trainee_internship.csv"
OUTPUT_JSON = "vagas_estritamente_junior_trainee_internship.json"


def sort_jobs_by_rating(jobs: List[JobPost]) -> List[JobPost]:
    """Ordenacao unica usada pelo terminal, exports e email."""
    def rating(job: JobPost) -> float:
        try:
            value = float(job.rating_score or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    return sorted(
        jobs,
        key=lambda job: (-rating(job), job.company.casefold(), job.title.casefold(), job.job_url),
    )

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
        console.print("[bold cyan]>>> A recolher vagas Junior / Trainee / Internship (IA/ML + Top Tech SWE)...[/bold cyan]\n")

        tasks = [
            # 1. LinkedIn Portugal (IA/ML + Top Tech Internships)
            self.linkedin.fetch("Junior AI Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Engineer Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Gen AI Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Artificial Intelligence Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Machine Learning Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Machine Learning Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Internship", "Portugal", total_wanted=30),
            self.linkedin.fetch("NLP Junior", "Portugal", total_wanted=30),
            self.linkedin.fetch("NLP Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Data Scientist", "Portugal", total_wanted=30),
            self.linkedin.fetch("Data Science Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Software Engineer Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Software Engineering Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Systems Engineer Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Software Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior AI", "Lisbon, Portugal", total_wanted=30),
            self.linkedin.fetch("Software Intern", "Lisbon, Portugal", total_wanted=30),

            # 2. ITJobs Portugal (Mercado Português / Bolsas / IEFP)
            self.itjobs.fetch("junior", max_pages=2),
            self.itjobs.fetch("inteligencia artificial", max_pages=2),
            self.itjobs.fetch("machine learning", max_pages=2),
            self.itjobs.fetch("estagio", max_pages=2),
            self.itjobs.fetch("ai engineer", max_pages=2),
            self.itjobs.fetch("nlp", max_pages=2),

            # 3. Landing.jobs (Portugal Tech Hub)
            self.landing_jobs.fetch("ai", limit=30),
            self.landing_jobs.fetch("machine learning", limit=30),
            self.landing_jobs.fetch("junior software", limit=30),
            self.landing_jobs.fetch("intern", limit=30),

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

        # Evita abrir dezenas de ligações simultaneas aos portais, sem perder a
        # recolha paralela entre fontes.
        semaphore = asyncio.Semaphore(8)

        async def run_limited(task):
            async with semaphore:
                return await task

        raw_responses = await asyncio.gather(
            *(run_limited(task) for task in tasks),
            return_exceptions=True,
        )

        all_jobs: List[JobPost] = []
        for source_index, res in enumerate(raw_responses, start=1):
            if isinstance(res, list):
                all_jobs.extend(res)
            elif isinstance(res, Exception):
                logger.warning("Fonte de vagas %s falhou: %s", source_index, res)

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
        final_jobs = [job for job in candidates if JobFilterEngine.is_eligible_after_rating(job)]

        # ORDENAÇÃO: Da empresa com MAIOR rating (ex: 4.4, 4.2, 3.7) até à menor / sem rating
        final_jobs = sort_jobs_by_rating(final_jobs)

        # Enriquecimento opcional: Hiring Manager Intelligence (apenas para as vagas de topo, mantendo a ordenação)
        console.print("[yellow]A processar Hiring Manager Intelligence para as vagas de topo...[/yellow]")
        await HiringIntelligence.enrich_jobs_async(final_jobs)

        console.print(f"[bold green]Total de vagas qualificadas (Ordenadas por Rating):[/bold green] {len(final_jobs)}\n")
        return final_jobs

    def export_and_display(self, jobs: List[JobPost]):
        jobs = sort_jobs_by_rating(jobs)

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

        # Exportar CSV e JSON mesmo quando a recolha nao devolve resultados. Assim,
        # uma execucao vazia nunca publica dados antigos como se fossem atuais.
        csv_filename = OUTPUT_CSV
        df = pd.DataFrame(data_rows)
        df.to_csv(csv_filename, index=False, quoting=csv.QUOTE_NONNUMERIC)

        json_filename = OUTPUT_JSON
        with open(json_filename, "w", encoding="utf-8") as f:
            json.dump(data_rows, f, ensure_ascii=False, indent=2)

        if not jobs:
            console.print("[yellow]Nenhuma vaga passou pelo filtro nesta execução.[/yellow]")
        else:
            console.print(f"\n[bold green]Ficheiro atualizado e ordenado por rating:[/bold green] {csv_filename}")
            console.print(f"[bold green]Ficheiro atualizado e ordenado por rating:[/bold green] {json_filename}")

        # Disparo de Email Notifier se configurado
        send_daily_email(json_filename, csv_filename)

if __name__ == "__main__":
    pipeline = AIJobPipeline()
    jobs = asyncio.run(pipeline.run())
    pipeline.export_and_display(jobs)
