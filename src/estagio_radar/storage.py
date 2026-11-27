"""Persistência SQLite para histórico, deteção first-mover e gestão de candidaturas."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from estagio_radar.date_utils import format_freshness
from estagio_radar.models import (
    CompanyProfile,
    EnrichedJob,
    ScoreBreakdown,
    SourceHealth,
)

VALID_USER_STATUSES = {"new", "saved", "applied", "interview", "ignored"}


class RadarStorage:
    """Base de dados SQLite local do Estágio Radar PT."""

    def __init__(self, db_path: str | Path = "data/radar.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    fingerprint TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    job_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    primary_source TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    is_remote INTEGER NOT NULL DEFAULT 0,
                    role_tier TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    geo_zone TEXT NOT NULL,
                    salary TEXT,
                    published_at TEXT,
                    published_date TEXT,
                    tags_json TEXT NOT NULL,
                    description_snippet TEXT,
                    applicant_count INTEGER,
                    company_profile_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    score_breakdown_json TEXT NOT NULL,
                    badges_json TEXT NOT NULL,
                    user_status TEXT NOT NULL DEFAULT 'new',
                    user_notes TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    notified_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_active_score
                    ON jobs(is_active, score DESC);

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    raw_count INTEGER NOT NULL,
                    accepted_count INTEGER NOT NULL,
                    new_count INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    source_health_json TEXT NOT NULL
                );
                """
            )
            for col_def in ("full_description TEXT", "external_apply_url TEXT"):
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass

    def get_job_details(self, fingerprint: str) -> dict[str, str | None]:
        """Devolve descrição completa e URL externa guardadas em cache na BD."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT full_description, external_apply_url FROM jobs WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return {"full_description": None, "external_apply_url": None}
        return {
            "full_description": row["full_description"],
            "external_apply_url": row["external_apply_url"],
        }

    def save_job_details(
        self,
        fingerprint: str,
        *,
        full_description: str | None = None,
        external_apply_url: str | None = None,
    ) -> None:
        """Guarda descrição completa e URL de candidatura externa em cache no SQLite."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET full_description = COALESCE(?, full_description),
                    external_apply_url = COALESCE(?, external_apply_url)
                WHERE fingerprint = ?
                """,
                (full_description or None, external_apply_url or None, fingerprint),
            )

    def upsert_jobs(
        self,
        jobs: list[EnrichedJob],
        *,
        deactivate_stale_full: bool = False,
        now: datetime | None = None,
    ) -> tuple[list[EnrichedJob], list[EnrichedJob]]:
        """Insere/atualiza vagas e devolve (todas_vistas_com_estado, vagas_ainda_nao_notificadas)."""
        ref = now or datetime.now(timezone.utc)
        now_iso = ref.isoformat()
        seen_fps = {j.fingerprint for j in jobs}

        all_updated: list[EnrichedJob] = []
        brand_new_unnotified: list[EnrichedJob] = []

        with self._connect() as conn:
            existing_rows = {
                row["fingerprint"]: dict(row)
                for row in conn.execute(
                    "SELECT fingerprint, user_status, user_notes, first_seen_at, notified_at FROM jobs"
                ).fetchall()
            }

            for job in jobs:
                prev = existing_rows.get(job.fingerprint)
                first_seen_iso = prev["first_seen_at"] if prev and prev["first_seen_at"] else now_iso
                user_status = prev["user_status"] if prev else "new"
                user_notes = prev["user_notes"] if prev else ""
                notified_iso = prev["notified_at"] if prev else None

                conn.execute(
                    """
                    INSERT INTO jobs (
                        fingerprint, title, company, location, job_url, canonical_url,
                        sources_json, primary_source, source_type, modality, is_remote,
                        role_tier, domain, geo_zone, salary, published_at, published_date,
                        tags_json, description_snippet, applicant_count,
                        company_profile_json, score, score_breakdown_json, badges_json,
                        user_status, user_notes, first_seen_at, last_seen_at, notified_at, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        title=excluded.title,
                        company=excluded.company,
                        location=excluded.location,
                        job_url=excluded.job_url,
                        canonical_url=excluded.canonical_url,
                        sources_json=excluded.sources_json,
                        primary_source=excluded.primary_source,
                        source_type=excluded.source_type,
                        modality=excluded.modality,
                        is_remote=excluded.is_remote,
                        role_tier=excluded.role_tier,
                        domain=excluded.domain,
                        geo_zone=excluded.geo_zone,
                        salary=COALESCE(excluded.salary, jobs.salary),
                        published_at=COALESCE(excluded.published_at, jobs.published_at),
                        published_date=excluded.published_date,
                        tags_json=excluded.tags_json,
                        description_snippet=excluded.description_snippet,
                        applicant_count=COALESCE(excluded.applicant_count, jobs.applicant_count),
                        company_profile_json=excluded.company_profile_json,
                        score=excluded.score,
                        score_breakdown_json=excluded.score_breakdown_json,
                        badges_json=excluded.badges_json,
                        last_seen_at=excluded.last_seen_at,
                        is_active=1
                    """,
                    (
                        job.fingerprint,
                        job.title,
                        job.company,
                        job.location,
                        job.job_url,
                        job.canonical_url,
                        json.dumps(job.sources, ensure_ascii=False),
                        job.primary_source,
                        job.source_type,
                        job.modality,
                        1 if job.is_remote else 0,
                        job.role_tier.value,
                        job.domain.value,
                        job.geo_zone.value,
                        job.salary,
                        job.published_at.isoformat() if job.published_at else None,
                        job.published_date,
                        json.dumps(job.tags, ensure_ascii=False),
                        job.description_snippet,
                        job.applicant_count,
                        job.company_profile.model_dump_json(),
                        job.score,
                        job.score_breakdown.model_dump_json(),
                        json.dumps(job.badges, ensure_ascii=False),
                        user_status,
                        user_notes,
                        first_seen_iso,
                        now_iso,
                        notified_iso,
                    ),
                )

                hydrated = job.model_copy(
                    update={
                        "user_status": user_status,
                        "user_notes": user_notes,
                        "first_seen_at": datetime.fromisoformat(first_seen_iso),
                        "last_seen_at": ref,
                        "notified_at": datetime.fromisoformat(notified_iso) if notified_iso else None,
                    }
                )
                all_updated.append(hydrated)
                if not notified_iso and user_status != "ignored":
                    brand_new_unnotified.append(hydrated)

            if deactivate_stale_full:
                # Só desativa vagas antigas (>48h sem serem vistas num scan completo)
                cutoff = (ref - timedelta(hours=48)).isoformat()
                rows = conn.execute(
                    "SELECT fingerprint, last_seen_at FROM jobs WHERE is_active = 1"
                ).fetchall()
                stale_fps = [
                    r["fingerprint"]
                    for r in rows
                    if r["fingerprint"] not in seen_fps and r["last_seen_at"] < cutoff
                ]
                if stale_fps:
                    conn.executemany(
                        "UPDATE jobs SET is_active = 0 WHERE fingerprint = ?",
                        [(fp,) for fp in stale_fps],
                    )

        return all_updated, brand_new_unnotified

    def mark_notified(self, fingerprints: list[str], now: datetime | None = None) -> None:
        if not fingerprints:
            return
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE jobs SET notified_at = ? WHERE fingerprint = ?",
                [(ts, fp) for fp in fingerprints],
            )

    def mark_all_active_notified(self, now: datetime | None = None) -> int:
        """Marca todas as vagas ativas ainda não notificadas como notificadas (baseline inicial)."""
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET notified_at = ? WHERE is_active = 1 AND notified_at IS NULL",
                (ts,),
            )
            return int(cur.rowcount or 0)

    def update_job_status(self, fingerprint: str, status: str, notes: str | None = None) -> bool:
        if status not in VALID_USER_STATUSES:
            raise ValueError(f"Estado inválido: {status}")
        with self._connect() as conn:
            if notes is None:
                cur = conn.execute(
                    "UPDATE jobs SET user_status = ? WHERE fingerprint = ?",
                    (status, fingerprint),
                )
            else:
                cur = conn.execute(
                    "UPDATE jobs SET user_status = ?, user_notes = ? WHERE fingerprint = ?",
                    (status, notes, fingerprint),
                )
            return cur.rowcount > 0

    def list_jobs(
        self,
        *,
        active_only: bool = True,
        hide_consultancies: bool = False,
        geo_zone: str | None = None,
        role_tier: str | None = None,
        domain: str | None = None,
        user_status: str | None = None,
        min_score: float = 0.0,
        limit: int = 500,
    ) -> list[EnrichedJob]:
        query = "SELECT * FROM jobs WHERE score >= ?"
        params: list[Any] = [min_score]
        if active_only:
            query += " AND is_active = 1"
        if geo_zone:
            query += " AND geo_zone = ?"
            params.append(geo_zone)
        if role_tier:
            query += " AND role_tier = ?"
            params.append(role_tier)
        if domain:
            query += " AND domain = ?"
            params.append(domain)
        if user_status:
            query += " AND user_status = ?"
            params.append(user_status)
        query += " ORDER BY score DESC, last_seen_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        out: list[EnrichedJob] = []
        now = datetime.now(timezone.utc)
        for r in rows:
            cp = CompanyProfile.model_validate_json(r["company_profile_json"])
            if hide_consultancies and cp.is_consultancy:
                continue
            out.append(self._row_to_enriched_job(r, cp=cp, now=now))
        return out

    def _row_to_enriched_job(
        self,
        r: sqlite3.Row,
        *,
        cp: CompanyProfile | None = None,
        now: datetime | None = None,
    ) -> EnrichedJob:
        ref = now or datetime.now(timezone.utc)
        profile = cp or CompanyProfile.model_validate_json(r["company_profile_json"])
        pub_dt = datetime.fromisoformat(r["published_at"]) if r["published_at"] else None
        return EnrichedJob(
            fingerprint=r["fingerprint"],
            title=r["title"],
            company=r["company"],
            location=r["location"],
            job_url=r["job_url"],
            canonical_url=r["canonical_url"],
            sources=json.loads(r["sources_json"]),
            primary_source=r["primary_source"],
            source_type=r["source_type"],
            modality=r["modality"],
            is_remote=bool(r["is_remote"]),
            role_tier=r["role_tier"],
            domain=r["domain"],
            geo_zone=r["geo_zone"],
            salary=r["salary"],
            published_at=pub_dt,
            published_date=r["published_date"] or "",
            freshness_label=format_freshness(pub_dt, ref),
            tags=json.loads(r["tags_json"]),
            description_snippet=r["description_snippet"] or "",
            applicant_count=r["applicant_count"],
            company_profile=profile,
            score=r["score"],
            score_breakdown=ScoreBreakdown.model_validate_json(r["score_breakdown_json"]),
            badges=json.loads(r["badges_json"]),
            user_status=r["user_status"],
            user_notes=r["user_notes"] or "",
            first_seen_at=datetime.fromisoformat(r["first_seen_at"]) if r["first_seen_at"] else None,
            last_seen_at=datetime.fromisoformat(r["last_seen_at"]) if r["last_seen_at"] else None,
            notified_at=datetime.fromisoformat(r["notified_at"]) if r["notified_at"] else None,
        )

    def get_job(self, fingerprint: str) -> EnrichedJob | None:
        """Obtém uma vaga específica pelo seu fingerprint."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if row is None:
            return None
        return self._row_to_enriched_job(row)

    def record_run(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        mode: str,
        raw_count: int,
        accepted_count: int,
        new_count: int,
        duration_ms: int,
        source_health: list[SourceHealth],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (started_at, finished_at, mode, raw_count, accepted_count, new_count, duration_ms, source_health_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at.isoformat(),
                    finished_at.isoformat(),
                    mode,
                    raw_count,
                    accepted_count,
                    new_count,
                    duration_ms,
                    json.dumps([s.model_dump() for s in source_health], ensure_ascii=False),
                ),
            )

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total_active = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0]
            by_status = {
                r["user_status"]: r["cnt"]
                for r in conn.execute(
                    "SELECT user_status, COUNT(*) AS cnt FROM jobs WHERE is_active = 1 GROUP BY user_status"
                ).fetchall()
            }
            by_zone = {
                r["geo_zone"]: r["cnt"]
                for r in conn.execute(
                    "SELECT geo_zone, COUNT(*) AS cnt FROM jobs WHERE is_active = 1 GROUP BY geo_zone"
                ).fetchall()
            }
            by_tier = {
                r["role_tier"]: r["cnt"]
                for r in conn.execute(
                    "SELECT role_tier, COUNT(*) AS cnt FROM jobs WHERE is_active = 1 GROUP BY role_tier"
                ).fetchall()
            }
            last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "total_active": total_active,
            "by_status": by_status,
            "by_zone": by_zone,
            "by_tier": by_tier,
            "last_run": dict(last_run) if last_run else None,
        }
