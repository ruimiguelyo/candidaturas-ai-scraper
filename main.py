import argparse
import json
import os
import sys

# Fix Windows console UTF-8 encoding
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

import asyncio
import logging
import math
from collections import Counter
from pathlib import Path
from typing import List
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
from exporter import export_public_jobs

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("AIJobPipeline")
console = Console(force_terminal=True, legacy_windows=False)

OUTPUT_CSV_NAME = "vagas_estritamente_junior_trainee_internship.csv"
OUTPUT_JSON_NAME = "vagas_estritamente_junior_trainee_internship.json"
MIN_SUCCESSFUL_REQUEST_RATIO = 0.25
MIN_HEALTHY_SOURCES = 2


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


def select_new_jobs(jobs: List[JobPost], previous_json_path: Path | str) -> List[JobPost]:
    """Compare with the last snapshot so the daily digest does not repeat every job."""
    previous_path = Path(previous_json_path)
    if not previous_path.exists():
        return list(jobs)
    try:
        previous_rows = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_keys = {
            JobPost.model_validate(row).deduplication_key()
            for row in previous_rows
            if isinstance(row, dict)
        }
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
        logger.warning("Snapshot anterior inválido; o digest incluirá todas as vagas: %s", error)
        return list(jobs)
    return [job for job in jobs if job.deduplication_key() not in previous_keys]

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
            self.jobicy.fetch(None, count=30),

            # 6. Arbeitnow & RemoteOK
            self.arbeitnow.fetch("junior machine learning", limit=40),
            self.arbeitnow.fetch("software engineer intern", limit=40),
            self.remoteok.fetch("intern", limit=40),
            self.remoteok.fetch("junior", limit=40)
        ]

        task_sources = (
            ["LinkedIn"] * 19
            + ["ITJobs.pt"] * 6
            + ["Landing.jobs"] * 4
            + ["Himalayas"] * 6
            + ["Jobicy"] * 3
            + ["Arbeitnow"] * 2
            + ["RemoteOK"] * 2
        )
        if len(task_sources) != len(tasks):
            raise RuntimeError("O plano de pesquisas e os respetivos nomes de fonte ficaram dessincronizados.")

        # Evita abrir dezenas de ligações simultaneas aos portais, sem perder a
        # recolha paralela entre fontes.
        semaphore = asyncio.Semaphore(8)
        source_semaphores = {
            "LinkedIn": asyncio.Semaphore(3),
            "ITJobs.pt": asyncio.Semaphore(2),
        }

        async def run_limited(source, task):
            async with semaphore:
                source_semaphore = source_semaphores.get(source)
                try:
                    if source_semaphore is None:
                        return source, await task, None
                    async with source_semaphore:
                        return source, await task, None
                except Exception as error:
                    return source, [], error

        raw_responses = await asyncio.gather(
            *(run_limited(source, task) for source, task in zip(task_sources, tasks)),
        )

        all_jobs: List[JobPost] = []
        successful_requests: Counter[str] = Counter()
        failed_requests: Counter[str] = Counter()
        jobs_by_source: Counter[str] = Counter()
        last_errors: dict[str, str] = {}

        for source, jobs, error in raw_responses:
            if error is not None:
                failed_requests[source] += 1
                last_errors[source] = str(error)
                continue
            successful_requests[source] += 1
            jobs_by_source[source] += len(jobs)
            all_jobs.extend(jobs)

        if not successful_requests:
            raise RuntimeError(
                "Todas as fontes falharam. Os últimos ficheiros válidos foram preservados e o email não foi enviado."
            )

        health_table = Table(title="SAÚDE DAS FONTES", show_lines=False)
        health_table.add_column("Fonte")
        health_table.add_column("Pesquisas OK", justify="right")
        health_table.add_column("Falhas", justify="right")
        health_table.add_column("Vagas brutas", justify="right")
        for source in dict.fromkeys(task_sources):
            health_table.add_row(
                source,
                str(successful_requests[source]),
                str(failed_requests[source]),
                str(jobs_by_source[source]),
            )
        console.print(health_table)
        for source, failure_count in failed_requests.items():
            logger.warning(
                "%s: %s pesquisa(s) falharam; último erro: %s",
                source,
                failure_count,
                last_errors[source],
            )

        minimum_requests = max(1, math.ceil(len(tasks) * MIN_SUCCESSFUL_REQUEST_RATIO))
        if len(successful_requests) < MIN_HEALTHY_SOURCES or sum(successful_requests.values()) < minimum_requests:
            raise RuntimeError(
                "A recolha ficou abaixo do limiar de saúde "
                f"({len(successful_requests)} fontes e {sum(successful_requests.values())}/{len(tasks)} pesquisas OK). "
                "Os últimos ficheiros válidos foram preservados e o email não foi enviado."
            )

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

    def export_and_display(
        self,
        jobs: List[JobPost],
        output_dir: Path | str = Path("."),
        notify_mode: str | None = None,
    ):
        jobs = sort_jobs_by_rating(jobs)
        output_directory = Path(output_dir)
        csv_path = output_directory / OUTPUT_CSV_NAME
        json_path = output_directory / OUTPUT_JSON_NAME
        new_jobs = select_new_jobs(jobs, json_path)

        table = Table(title="VAGAS QUALIFICADAS (ORDENADAS POR RATING DE EMPRESA)", show_lines=True)
        table.add_column("Score", style="bold yellow", width=16)
        table.add_column("Empresa", style="green", width=18)
        table.add_column("Título do Cargo", style="bold white", width=30)
        table.add_column("Categoria", style="cyan", width=12)
        table.add_column("Localização", style="magenta", width=18)
        table.add_column("Compat.", style="cyan", width=11)
        table.add_column("Regime", style="yellow", width=10)
        table.add_column("Link de Candidatura", style="blue", width=36)

        for j in jobs:
            score_display = f"{j.company_score} ({j.company_reviews})" if j.company_score else "Sem rating"
            table.add_row(
                score_display,
                j.company,
                j.title,
                j.category,
                j.location,
                j.location_compatibility,
                j.modality,
                j.job_url
            )

        console.print(table)

        # Public exports exclude private outreach data and third-party description
        # bodies. The private in-memory rows are used only for an explicitly
        # requested notification and are never written to the repository.
        export_public_jobs(jobs, csv_path, json_path)

        if not jobs:
            console.print("[yellow]Nenhuma vaga passou pelo filtro nesta execução.[/yellow]")
        else:
            console.print(f"\n[bold green]Ficheiro público atualizado:[/bold green] {csv_path}")
            console.print(f"[bold green]Ficheiro público atualizado:[/bold green] {json_path}")
            console.print(f"[bold cyan]Vagas novas desde o snapshot anterior:[/bold cyan] {len(new_jobs)}")

        if notify_mode:
            notification_jobs = jobs if notify_mode == "all" else new_jobs
            delivered = send_daily_email(
                str(json_path),
                str(csv_path),
                jobs=[job.model_dump() for job in notification_jobs],
                new_only=notify_mode == "new",
            )
            if not delivered:
                raise RuntimeError("A notificação por email foi pedida, mas não foi entregue.")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Recolhe e filtra vagas técnicas entry-level sem efetuar candidaturas automaticamente."
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("OUTPUT_DIR", "."),
        help="Diretório dos exports públicos (predefinição: diretório atual).",
    )
    notification_group = parser.add_mutually_exclusive_group()
    notification_group.add_argument(
        "--notify",
        action="store_true",
        help="Envia por email apenas as vagas novas desde o último snapshot.",
    )
    notification_group.add_argument(
        "--notify-all",
        action="store_true",
        help="Envia todas as vagas atuais, mesmo que já existissem no snapshot anterior.",
    )
    parser.add_argument(
        "--outreach",
        action="store_true",
        help="Ativa a pesquisa privada de contactos; exige um perfil local e verificação humana.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=os.getenv("LOG_LEVEL", "WARNING").upper(),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.getLogger().setLevel(args.log_level)
    if args.outreach:
        os.environ["HIRING_INTELLIGENCE_ENABLED"] = "true"

    pipeline = AIJobPipeline()
    jobs = asyncio.run(pipeline.run())
    notify_mode = "all" if args.notify_all else ("new" if args.notify else None)
    pipeline.export_and_display(jobs, output_dir=args.output_dir, notify_mode=notify_mode)


if __name__ == "__main__":
    main()
