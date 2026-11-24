"""Modelos Pydantic centrais do Estágio Radar PT."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class RoleTier(str, Enum):
    INTERNSHIP = "INTERNSHIP"
    ENTRY_JUNIOR = "ENTRY_JUNIOR"


class GeoZone(str, Enum):
    SETUBAL_LOCAL = "SETUBAL_LOCAL"
    LISBOA_METRO = "LISBOA_METRO"
    REMOTE_PT = "REMOTE_PT"
    PT_UNSPECIFIED = "PT_UNSPECIFIED"
    PT_OTHER_CITY = "PT_OTHER_CITY"
    REJECTED_GEO = "REJECTED_GEO"


class DomainCategory(str, Enum):
    AI_ML = "AI_ML"
    DATA = "DATA"
    SWE = "SWE"
    CYBER = "CYBER"
    CLOUD_DEVOPS = "CLOUD_DEVOPS"
    EMBEDDED = "EMBEDDED"


class CompanyType(str, Enum):
    PRODUCT_TECH = "PRODUCT_TECH"
    ENTERPRISE_FINANCE = "ENTERPRISE_FINANCE"
    RESEARCH_ACADEMIC = "RESEARCH_ACADEMIC"
    CONSULTANCY = "CONSULTANCY"
    UNKNOWN = "UNKNOWN"


class RawJob(BaseModel):
    """Registo normalizado devolvido por qualquer scraper antes da filtragem."""

    source: str
    source_type: str = "board"  # "ats" | "pt_board" | "linkedin" | "remote_eu"
    job_id: str
    title: str
    company: str
    location: str = "Unknown"
    all_locations: list[str] = Field(default_factory=list)
    job_url: str
    modality: str = "Unknown"  # "On-site", "Hybrid", "Remote", "Unknown"
    is_remote: bool = False
    employment_type: Optional[str] = None
    seniority: Optional[str] = None
    salary: Optional[str] = None
    published_at: Optional[datetime] = None
    published_raw: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    applicant_count: Optional[int] = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    teamlyzer_hint: Optional[dict[str, Any]] = None


class CompanyProfile(BaseModel):
    """Perfil de reputação e tipo de empresa enriquecido via Teamlyzer + config."""

    company_name: str
    normalized_name: str
    teamlyzer_slug: Optional[str] = None
    teamlyzer_url: Optional[str] = None
    teamlyzer_rating: Optional[float] = None
    teamlyzer_reviews: int = 0
    bayesian_rating: Optional[float] = None
    avg_salary_eur: Optional[int] = None
    teamlyzer_categories: list[str] = Field(default_factory=list)
    is_consultancy: bool = False
    company_tier: str = "UNKNOWN"  # "S" | "A" | "B" | "CONSULTANCY" | "UNKNOWN"
    company_type: CompanyType = CompanyType.UNKNOWN
    checked_at: Optional[int] = None


class ScoreBreakdown(BaseModel):
    """Decomposição transparente do score 0-100."""

    total: float = 0.0
    role_points: float = 0.0
    company_points: float = 0.0
    location_points: float = 0.0
    freshness_points: float = 0.0
    notes: list[str] = Field(default_factory=list)


class FilterDecision(BaseModel):
    """Resultado auditável do motor de filtragem."""

    accepted: bool
    reason_code: str
    reason_detail: str
    role_tier: Optional[RoleTier] = None
    domain: Optional[DomainCategory] = None
    geo_zone: GeoZone = GeoZone.REJECTED_GEO


class EnrichedJob(BaseModel):
    """Oportunidade final validada, deduplicada, classificada e pronta para alertas/UI."""

    fingerprint: str
    title: str
    company: str
    location: str
    job_url: str
    canonical_url: str
    sources: list[str] = Field(default_factory=list)
    primary_source: str
    source_type: str
    modality: str = "Unknown"
    is_remote: bool = False
    role_tier: RoleTier
    domain: DomainCategory
    geo_zone: GeoZone
    salary: Optional[str] = None
    published_at: Optional[datetime] = None
    published_date: str = ""
    freshness_label: str = ""
    tags: list[str] = Field(default_factory=list)
    description_snippet: str = ""
    applicant_count: Optional[int] = None
    company_profile: CompanyProfile
    score: float = 0.0
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    badges: list[str] = Field(default_factory=list)
    user_status: str = "new"  # new | saved | applied | interview | ignored
    user_notes: str = ""
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None


class SourceHealth(BaseModel):
    """Métricas de saúde por fonte durante uma execução."""

    source: str
    category: str = "board"
    requests_ok: int = 0
    requests_partial: int = 0
    requests_failed: int = 0
    raw_count: int = 0
    accepted_count: int = 0
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
