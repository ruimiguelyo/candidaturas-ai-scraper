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
from scrapers.remotive import RemotiveScraper
from filter_engine import JobFilterEngine
from company_ranker import CompanyRanker
from hiring_intelligence import HiringIntelligence
from email_notifier import send_daily_email
from exporter import export_public_artifacts

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("AIJobPipeline")
console = Console(force_terminal=True, legacy_windows=False)

OUTPUT_CSV_NAME = "vagas_estritamente_junior_trainee_internship.csv"
OUTPUT_JSON_NAME = "vagas_estritamente_junior_trainee_internship.json"
OUTPUT_REJECTIONS_NAME = "vagas_rejeitadas.csv"
MIN_SUCCESSFUL_REQUEST_RATIO = 0.25
MIN_HEALTHY_SOURCES = 2
MIN_UNIQUE_RAW_JOBS = 5

LINKEDIN_SEARCHES = (
    "Junior AI Engineer",
    "AI Engineer Intern",
    "Machine Learning Intern",
    "Junior Machine Learning Engineer",
    "NLP Intern",
    "Computer Vision Intern",
    "MLOps Intern",
    "Junior Data Scientist",
    "Data Science Intern",
    "Junior Data Engineer",
    "Data Analyst Intern",
    "AI Research Intern",
    "Prompt Engineering Intern",
    "RAG Engineer Intern",
    "Software Engineer Intern",
    "Junior Software Engineer",
    "Graduate Software Engineer",
    "Entry Level Software Engineer",
    "Associate Software Engineer",
    "Frontend Developer Intern",
    "Backend Developer Intern",
    "Full Stack Developer Intern",
    "Cloud Engineer Intern",
    "DevOps Intern",
    "QA Test Automation Intern",
    "Cybersecurity Intern",
    "Platform SRE Intern",
    "Mobile Developer Intern",
    "Python Developer Intern",
    "Embedded Engineer Intern",
    "Early Career Software Engineer",
    "Software Engineering Apprentice",
    "Software Engineering Academy",
    "Level I Software Engineer",
    "Junior Web Application Developer",
)
ITJOBS_SEARCHES = (
    "junior",
    "estagio",
    "trainee",
    "graduate",
    "recem licenciado",
)
LANDING_JOBS_SEARCHES = (
    "ai",
    "machine learning",
    "data",
    "junior",
    "graduate",
    "intern",
    "developer",
    "cybersecurity",
)
HIMALAYAS_SEARCHES = (
    "Junior AI",
    "AI Intern",
    "Machine Learning Intern",
    "Junior Data",
    "Data Intern",
    "Software Engineer Intern",
    "Junior Software Engineer",
    "Graduate Software Engineer",
    "Entry Level Software",
    "Cybersecurity Intern",
)
JOBICY_SEARCHES = (
    "artificial intelligence",
    "machine learning",
    "data science",
    "software engineer",
    "cybersecurity",
    None,
)


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
        self.remotive = RemotiveScraper()
        self.rejections: list[dict] = []

    @staticmethod
    def _rejection_record(job: JobPost, stage: str, reason_code: str, reason_detail: str) -> dict:
        return {
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "job_url": job.job_url,
            "rejection_stage": stage,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
        }

    @staticmethod
    def _job_quality(job: JobPost) -> tuple[int, int, int, int]:
        """Prefer the richest version when the same advert appears in several searches."""
        meaningful_fields = (
            job.company,
            job.location,
            job.seniority,
            job.salary,
            job.post_date,
        )
        metadata_score = sum(
            bool(str(value or "").strip())
            and str(value).strip().casefold() not in {"unknown", "n/a", "na"}
            for value in meaningful_fields
        )
        return (
            metadata_score,
            len(job.tags or []),
            len(str(job.description_snippet or "")),
            len(str(job.job_url or "")),
        )

    def _filter_unique_jobs(self, all_jobs: List[JobPost]) -> List[JobPost]:
        """Evaluate one vacancy identity once, using every collected variant as evidence."""
        grouped: dict[str, list[JobPost]] = {}
        for job in all_jobs:
            grouped.setdefault(job.deduplication_key(), []).append(job)

        candidates: List[JobPost] = []
        rejection_priority = {
            "excluded_company": 0,
            "outside_recency_window": 1,
            "missing_post_date": 2,
            "invalid_post_date": 3,
            "excluded_seniority": 4,
            "excluded_non_technical": 5,
            "unsupported_domain": 6,
            "missing_entry_level_signal": 7,
        }

        for variants in grouped.values():
            evaluated = [(job, JobFilterEngine.evaluate_job(job)) for job in variants]
            source_names = sorted({job.source for job in variants})
            duplicate_suffix = (
                f" {len(variants)} ocorrências consolidadas de "
                f"{len(source_names)} fonte(s): {', '.join(source_names)}."
                if len(variants) > 1
                else ""
            )

            # Deloitte is an absolute policy exclusion, even if another copy of
            # the same URL has incomplete company metadata.
            company_exclusions = [
                pair for pair in evaluated if pair[1].reason_code == "excluded_company"
            ]
            if company_exclusions:
                rejected_job, decision = company_exclusions[0]
                self.rejections.append(
                    self._rejection_record(
                        rejected_job,
                        "semantic_filter",
                        decision.reason_code,
                        decision.reason_detail + duplicate_suffix,
                    )
                )
                continue

            accepted = [pair for pair in evaluated if pair[1].accepted and pair[1].job is not None]
            if accepted:
                selected_job, _decision = max(
                    accepted,
                    key=lambda pair: self._job_quality(pair[0]),
                )
                candidates.append(selected_job)
                if len(variants) > 1:
                    self.rejections.append(
                        self._rejection_record(
                            selected_job,
                            "deduplication",
                            "duplicate",
                            (
                                f"{len(variants)} ocorrências do mesmo anúncio foram consolidadas; "
                                f"foi mantida a versão de {selected_job.source}."
                            ),
                        )
                    )
                continue

            rejected_job, decision = min(
                evaluated,
                key=lambda pair: rejection_priority.get(pair[1].reason_code, 99),
            )
            self.rejections.append(
                self._rejection_record(
                    rejected_job,
                    "semantic_filter",
                    decision.reason_code,
                    decision.reason_detail + duplicate_suffix,
                )
            )

        return candidates

    async def run(self) -> List[JobPost]:
        console.print("[bold cyan]>>> A recolher vagas Junior / Trainee / Internship (IA/ML + Top Tech SWE)...[/bold cyan]\n")

        search_plan = [
            *(
                ("LinkedIn", self.linkedin.fetch(query, "Portugal", total_wanted=20))
                for query in LINKEDIN_SEARCHES
            ),
            *(("ITJobs.pt", self.itjobs.fetch(query, max_pages=3)) for query in ITJOBS_SEARCHES),
            *(
                ("Landing.jobs", self.landing_jobs.fetch(query, limit=60, max_pages=3))
                for query in LANDING_JOBS_SEARCHES
            ),
            *(("Himalayas", self.himalayas.fetch(query, limit=60)) for query in HIMALAYAS_SEARCHES),
            *(("Jobicy", self.jobicy.fetch(query, count=50)) for query in JOBICY_SEARCHES),
            ("Arbeitnow", self.arbeitnow.fetch(None, limit=250, max_pages=5)),
            ("RemoteOK", self.remoteok.fetch(None, limit=250)),
            ("Remotive", self.remotive.fetch(limit=200)),
        ]
        task_sources = [source for source, _task in search_plan]
        tasks = [task for _source, task in search_plan]
        self.rejections = []

        # Evita abrir dezenas de ligações simultaneas aos portais, sem perder a
        # recolha paralela entre fontes.
        semaphore = asyncio.Semaphore(8)
        source_semaphores = {
            "LinkedIn": asyncio.Semaphore(1),
            "ITJobs.pt": asyncio.Semaphore(2),
        }

        async def run_limited(source, task):
            source_semaphore = source_semaphores.get(source)

            async def execute():
                async with semaphore:
                    return await task

            try:
                if source_semaphore is None:
                    jobs = await execute()
                else:
                    # Acquire the narrow source limit first so queued LinkedIn/ITJobs
                    # work cannot occupy every global slot.
                    async with source_semaphore:
                        jobs = await execute()
                return (
                    source,
                    jobs,
                    None,
                    bool(getattr(jobs, "partial", False)),
                    getattr(jobs, "warning", None),
                )
            except Exception as error:
                return source, [], error, False, None

        raw_responses = await asyncio.gather(
            *(run_limited(source, task) for source, task in zip(task_sources, tasks)),
        )

        all_jobs: List[JobPost] = []
        successful_requests: Counter[str] = Counter()
        partial_requests: Counter[str] = Counter()
        failed_requests: Counter[str] = Counter()
        jobs_by_source: Counter[str] = Counter()
        last_errors: dict[str, str] = {}

        partial_warnings: dict[str, str] = {}
        for source, jobs, error, partial, warning in raw_responses:
            if error is not None:
                failed_requests[source] += 1
                last_errors[source] = str(error)
                continue
            if partial:
                partial_requests[source] += 1
                if warning:
                    partial_warnings[source] = str(warning)
            else:
                successful_requests[source] += 1
            jobs_by_source[source] += len(jobs)
            all_jobs.extend(jobs)

        completed_requests = sum(successful_requests.values()) + sum(partial_requests.values())
        if completed_requests == 0:
            raise RuntimeError(
                "Todas as fontes falharam. Os últimos ficheiros válidos foram preservados e o email não foi enviado."
            )

        health_table = Table(title="SAÚDE DAS FONTES", show_lines=False)
        health_table.add_column("Fonte")
        health_table.add_column("Pesquisas OK", justify="right")
        health_table.add_column("Parciais", justify="right")
        health_table.add_column("Falhas", justify="right")
        health_table.add_column("Vagas brutas", justify="right")
        for source in dict.fromkeys(task_sources):
            health_table.add_row(
                source,
                str(successful_requests[source]),
                str(partial_requests[source]),
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
        for source, warning in partial_warnings.items():
            logger.warning("%s", warning)

        minimum_requests = max(1, math.ceil(len(tasks) * MIN_SUCCESSFUL_REQUEST_RATIO))
        sources_with_jobs = {source for source, count in jobs_by_source.items() if count > 0}
        unique_raw_jobs = len({job.deduplication_key() for job in all_jobs})
        if (
            len(sources_with_jobs) < MIN_HEALTHY_SOURCES
            or completed_requests < minimum_requests
            or unique_raw_jobs < MIN_UNIQUE_RAW_JOBS
        ):
            raise RuntimeError(
                "A recolha ficou abaixo do limiar de saúde "
                f"({len(sources_with_jobs)} fontes com vagas, {unique_raw_jobs} vagas brutas únicas e "
                f"{completed_requests}/{len(tasks)} pesquisas completas ou parciais). "
                "Os últimos ficheiros válidos foram preservados e o email não foi enviado."
            )

        candidates = self._filter_unique_jobs(all_jobs)
        if not candidates:
            raise RuntimeError(
                "A recolha foi saudável, mas nenhuma vaga passou pelo filtro central. "
                "Os últimos ficheiros válidos foram preservados e o email não foi enviado."
            )

        # Cruzamento assíncrono com Teamlyzer e Glassdoor
        console.print("[yellow]A cruzar empresas em paralelo com scores do Teamlyzer e Glassdoor...[/yellow]")
        await CompanyRanker.enrich_jobs_async(candidates)

        # O rating e a localização são informação para ordenar e decidir; nunca
        # apagam uma vaga técnica entry-level já qualificada.
        final_jobs = candidates

        # ORDENAÇÃO: Da empresa com MAIOR rating (ex: 4.4, 4.2, 3.7) até à menor / sem rating
        final_jobs = sort_jobs_by_rating(final_jobs)

        # Enriquecimento opcional: Hiring Manager Intelligence (apenas para as vagas de topo, mantendo a ordenação)
        console.print("[yellow]A processar Hiring Manager Intelligence para as vagas de topo...[/yellow]")
        await HiringIntelligence.enrich_jobs_async(final_jobs)

        console.print(f"[bold green]Total de vagas qualificadas (Ordenadas por Rating):[/bold green] {len(final_jobs)}\n")
        rejection_counts = Counter(row["reason_code"] for row in self.rejections)
        if rejection_counts:
            rejection_table = Table(title="AUDITORIA DE REJEIÇÕES", show_lines=False)
            rejection_table.add_column("Motivo")
            rejection_table.add_column("Quantidade", justify="right")
            for reason_code, count in sorted(rejection_counts.items()):
                rejection_table.add_row(reason_code, str(count))
            console.print(rejection_table)
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
        rejections_path = output_directory / OUTPUT_REJECTIONS_NAME
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
        export_public_artifacts(
            jobs,
            self.rejections,
            csv_path,
            json_path,
            rejections_path,
        )

        if not jobs:
            console.print("[yellow]Nenhuma vaga passou pelo filtro nesta execução.[/yellow]")
        else:
            console.print(f"\n[bold green]Ficheiro público atualizado:[/bold green] {csv_path}")
            console.print(f"[bold green]Ficheiro público atualizado:[/bold green] {json_path}")
            console.print(f"[bold cyan]Vagas novas desde o snapshot anterior:[/bold cyan] {len(new_jobs)}")
        console.print(f"[bold green]Auditoria de rejeições atualizada:[/bold green] {rejections_path}")

        if notify_mode:
            # Email is the primary product: the normal digest contains the full
            # verified seven-day window, not only rows absent from yesterday's snapshot.
            notification_jobs = jobs if notify_mode in {"weekly", "all"} else new_jobs
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
        help="Envia por email todas as vagas verificadas dos últimos 7 dias.",
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
    notify_mode = "all" if args.notify_all else ("weekly" if args.notify else None)
    pipeline.export_and_display(jobs, output_dir=args.output_dir, notify_mode=notify_mode)


if __name__ == "__main__":
    main()
