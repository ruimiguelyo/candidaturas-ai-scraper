import math
import re
from typing import Optional, Tuple

from models import JobPost


class JobFilterEngine:
    """Aplica as regras de cargo antes e depois do enriquecimento de ratings."""

    AI_KEYWORDS = [
        r"\bai\b",
        r"\bml\b",
        r"\bnlp\b",
        r"\brag\b",
        r"\bllms?\b",
        r"\bartificial intelligence\b",
        r"\bintelig[eê]ncia artificial\b",
        r"\bmachine learning\b",
        r"\bdeep learning\b",
        r"\blarge language models?\b",
        r"\bgenerative ai\b",
        r"\bgen[- ]?ai\b",
        r"\bnatural language processing\b",
        r"\bcomputer vision\b",
        r"\bvis[aã]o computacional\b",
        r"\bdata science\b",
        r"\bdata scientist\b",
        r"\bdata engineer(?:ing)?\b",
        r"\bdata analyst\b",
        r"\bci[eê]ncia de dados\b",
        r"\bcientista de dados\b",
        r"\bengenheiro de dados\b",
        r"\btransformers?\b",
        r"\bprompt engineer(?:ing)?\b",
        r"\bmlops\b",
        r"\bai engineer(?:ing)?\b",
        r"\bresearch scientist\b",
        r"\binvestigador\b",
        r"\btech\s*&\s*ai\b",
    ]

    SOFTWARE_ENG_KEYWORDS = [
        r"\bsoftware engineer(?:ing)?\b",
        r"\bengenheiro de software\b",
        r"\bsoftware developer\b",
        r"\bdesenvolvedor de software\b",
        r"\bsystems engineer(?:ing)?\b",
        r"\bengenheiro de sistemas\b",
        r"\bbackend\b",
        r"\bback[- ]end\b",
        r"\bfull[- ]?stack\b",
        r"\bcloud engineer(?:ing)?\b",
        r"\bdevops\b",
        r"\binfrastructure\b",
        r"\bqa engineer(?:ing)?\b",
        r"\bsoftware development\b",
    ]

    # Estes sao os unicos marcadores que podem tornar um titulo elegivel.
    STRICT_TITLE_KEYWORDS = [
        r"\bjunior\b",
        r"\bj[uú]nior\b",
        r"\bjr\.?\b",
        r"\btrainee\b",
        r"\bintern(?:ship)?\b",
        r"\best[aá]gio\b",
        r"\bestagi[aá]rio\b",
        r"\bgraduate\b",
        r"\bentry[- ]level\b",
        r"\bearly[- ]career\b",
        r"\bstudent\b",
        r"\bbolseir[oa]\b",
        r"\bbolsa\s+de\s+investiga[cç][aã]o\b",
    ]

    # A exclusao e sempre aplicada, mesmo quando o titulo tambem diz Junior.
    EXCLUDE_KEYWORDS = [
        r"\bsenior\b",
        r"\bsr\.?\b",
        r"\blead\b",
        r"\bprincipal\b",
        r"\bstaff\b",
        r"\bmid[- ]level\b",
        r"\bpleno\b",
        r"\bjr\s*/\s*pl\b",
        r"\bdirector\b",
        r"\bhead\s+of\b",
        r"\bmanager\b",
        r"\bgerente\b",
    ]

    @staticmethod
    def _matches_any(text: str, patterns: list) -> bool:
        text = str(text or "")
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    @classmethod
    def is_ai_related(cls, text: str) -> bool:
        return cls._matches_any(text, cls.AI_KEYWORDS)

    @classmethod
    def is_software_eng_related(cls, text: str) -> bool:
        return cls._matches_any(text, cls.SOFTWARE_ENG_KEYWORDS)

    @classmethod
    def has_strictly_entry_level_title(cls, title: str) -> bool:
        return cls._matches_any(title, cls.STRICT_TITLE_KEYWORDS)

    @classmethod
    def is_excluded(cls, title: str, company: str) -> bool:
        title_text = str(title or "")
        company_text = str(company or "")
        if re.search(r"deloitte", company_text, re.IGNORECASE) or re.search(
            r"deloitte", title_text, re.IGNORECASE
        ):
            return True
        return cls._matches_any(title_text, cls.EXCLUDE_KEYWORDS)

    @classmethod
    def _classify_title(cls, title: str, tags: list) -> Optional[str]:
        """Classifica o cargo pelo nome, sem deixar texto promocional contornar o rating."""
        title_is_ai = cls.is_ai_related(title)
        title_is_swe = cls.is_software_eng_related(title)

        # Um titulo explicitamente SWE continua a ser SWE se apenas a descricao
        # ou tags mencionarem AI de forma incidental. AI Software Engineer e o
        # caso intencionalmente hibrido e entra como AI.
        if title_is_ai:
            return "AI"
        if title_is_swe:
            return "SWE"

        tag_text = " ".join(str(tag) for tag in (tags or []))
        if cls.is_ai_related(tag_text):
            return "AI"
        if cls.is_software_eng_related(tag_text):
            return "SWE"
        return None

    @classmethod
    def pre_filter_job(cls, job: JobPost) -> Optional[Tuple[JobPost, str]]:
        """Aceita apenas cargos entry-level explicitos e categorias suportadas."""
        title_text = job.title.strip()

        if cls.is_excluded(title_text, job.company):
            return None
        if not cls.has_strictly_entry_level_title(title_text):
            return None

        domain_type = cls._classify_title(title_text, job.tags)
        if domain_type == "AI":
            job.category = "AI / ML"
            job.seniority = "Junior / Trainee / Internship"
            return job, domain_type
        if domain_type == "SWE":
            job.category = "Top-Tier Software Engineering"
            job.seniority = "Junior / Trainee / Internship"
            return job, domain_type
        return None

    @staticmethod
    def is_eligible_after_rating(job: JobPost) -> bool:
        if job.category == "AI / ML":
            return True
        try:
            rating = float(job.rating_score or 0.0)
        except (TypeError, ValueError):
            return False
        return job.category == "Top-Tier Software Engineering" and math.isfinite(rating) and rating >= 3.1
