from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Any
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

class JobPost(BaseModel):
    source: str
    job_id: str
    title: str
    company: str
    location: str
    job_url: str
    modality: str = "Unknown"  # Remote, Hybrid, On-site
    seniority: Optional[str] = None
    is_remote: bool = False
    salary: Optional[str] = None
    post_date: Optional[str] = None
    company_score: Optional[str] = None        # Ex: "★ 3.7/5"
    company_reviews: Optional[str] = None      # Ex: "147 Reviews"
    teamlyzer_url: Optional[str] = None        # Link direto para as reviews
    rating_score: float = 0.0                  # Nota numérica para ordenação (ex: 4.1, 3.7, 3.1)
    category: str = "AI / ML"                  # "AI / ML" ou "Top-Tier Software Engineering"
    tags: List[str] = Field(default_factory=list)
    description_snippet: Optional[str] = None
    human_outreach: Optional[dict] = None      # Hiring Manager Intelligence outreach enrichment
    fetched_at: str = Field(default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    @field_validator("seniority", mode="before")
    @classmethod
    def convert_seniority(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    @field_validator("post_date", mode="before")
    @classmethod
    def convert_post_date(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

    def deduplication_key(self) -> str:
        """Prefere a identidade da oferta e nao empresa+titulo apenas."""
        if self.job_url:
            try:
                parsed = urlsplit(self.job_url.strip())
                if parsed.netloc and parsed.path:
                    canonical_url = urlunsplit(
                        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
                    )
                    return f"url:{canonical_url}"
            except ValueError:
                pass

        clean_source = "".join(c for c in self.source.casefold() if c.isalnum())
        clean_id = "".join(c for c in self.job_id.casefold() if c.isalnum())
        clean_company = "".join(c for c in self.company.casefold() if c.isalnum())
        clean_title = "".join(c for c in self.title.casefold() if c.isalnum())
        return f"job:{clean_source}:{clean_id}:{clean_company}:{clean_title}"
