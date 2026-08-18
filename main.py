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
        console.print("[bold cyan]>>> A recolher vagas Junior / Trainee / Internship (LinkedIn, Landing.jobs, Himalayas, ITJobs, Jobicy, Arbeitnow, RemoteOK)...[/bold cyan]\n")

        tasks = [
            # 1. LinkedIn Portugal & Remote
            self.linkedin.fetch("Junior AI Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Machine Learning Engineer", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Internship", "Portugal", total_wanted=30),
            self.linkedin.fetch("Machine Learning Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Machine Learning Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Gen AI Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("AI Trainee", "Portugal", total_wanted=30),
            self.linkedin.fetch("Estágio Inteligência Artificial", "Portugal", total_wanted=30),
            self.linkedin.fetch("Estágio Machine Learning", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior Data Scientist", "Portugal", total_wanted=30),
            self.linkedin.fetch("Data Science Intern", "Portugal", total_wanted=30),
            self.linkedin.fetch("Junior AI", "Lisbon, Portugal", total_wanted=30),
            self.linkedin.fetch("AI Intern", "Lisbon, Portugal", total_wanted=30),

            # 2. Landing.jobs (Portugal Tech Hub)
            self.landing_jobs.fetch("ai", limit=30),
            self.landing_jobs.fetch("machine learning", limit=30),
            self.landing_jobs.fetch("junior", limit=30),

            # 3. ITJobs Portugal
            self.itjobs.fetch("junior", limit=50),
            self.itjobs.fetch("estagio", limit=50),
            self.itjobs.fetch("ai", limit=50),

            # 4. Himalayas (Global Remote)
            self.himalayas.fetch("Junior AI", limit=40),
            self.himalayas.fetch("AI Intern", limit=40),
            self.himalayas.fetch("Machine Learning Intern", limit=40),
            self.himalayas.fetch("Junior Machine Learning", limit=40),
            self.himalayas.fetch("Junior Data", limit=40),

            # 5. Jobicy (Remote)
            self.jobicy.fetch("ai", count=30),
            self.jobicy.fetch("data", count=30),

            # 6. Arbeitnow & RemoteOK
            self.arbeitnow.fetch("junior machine learning", limit=40),
            self.arbeitnow.fetch("ai intern", limit=40),
            self.remoteok.fetch("intern", limit=40),
            self.remoteok.fetch("junior", limit=40)
        ]

        raw_responses = await asyncio.gather(*tasks, return_exceptions=True)

        all_jobs: List[JobPost] = []
        for res in raw_responses:
            if isinstance(res, list):
                all_jobs.extend(res)

        # Filtragem Estrita
        seen_keys = set()
        strictly_filtered: List[JobPost] = []

        for job in all_jobs:
            valid_job = JobFilterEngine.filter_job(job)
            if not valid_job:
                continue

            key = valid_job.deduplication_key()
            if key not in seen_keys:
                seen_keys.add(key)
                strictly_filtered.append(valid_job)

        console.print(f"[bold green]Total de vagas 100% Junior / Trainee / Internship confirmadas:[/bold green] {len(strictly_filtered)}\n")
        return strictly_filtered

    def export_and_display(self, jobs: List[JobPost]):
        if not jobs:
            console.print("[yellow]Nenhuma vaga passou pelo filtro estrito nesta execução.[/yellow]")
            return

        table = Table(title="VAGAS: JUNIOR / TRAINEE / INTERNSHIP EM IA & ML", show_lines=True)
        table.add_column("Fonte", style="cyan", width=12)
        table.add_column("Título do Cargo", style="bold white", width=34)
        table.add_column("Empresa", style="green", width=20)
        table.add_column("Localização", style="magenta", width=20)
        table.add_column("Regime", style="yellow", width=12)
        table.add_column("Link de Candidatura", style="blue", width=42)

        data_rows = []
        for j in jobs:
            table.add_row(
                j.source,
                j.title,
                j.company,
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

        console.print(f"\n[bold green]Ficheiro atualizado:[/bold green] {csv_filename}")
        console.print(f"[bold green]Ficheiro atualizado:[/bold green] {json_filename}")

if __name__ == "__main__":
    pipeline = AIJobPipeline()
    jobs = asyncio.run(pipeline.run())
    pipeline.export_and_display(jobs)
