import re
from typing import List, Optional
from models import JobPost

class JobFilterEngine:
    # Termos de IA com correspondência rigorosa de limites de palavra (\b)
    AI_KEYWORDS = [
        r"\bai\b", r"\bml\b", r"\bnlp\b", r"\brag\b", r"\bllm\b", r"\bllms\b",
        r"\bartificial intelligence\b", r"\binteligência artificial\b", r"\binteligencia artificial\b",
        r"\bmachine learning\b", r"\bdeep learning\b", r"\blarge language models?\b",
        r"\bgenerative ai\b", r"\bgenai\b", r"\bgen-ai\b",
        r"\bnatural language processing\b", r"\bcomputer vision\b", r"\bvisão computacional\b",
        r"\bdata science\b", r"\bdata scientist\b", r"\bciência de dados\b", r"\bcientista de dados\b",
        r"\btransformers?\b", r"\bprompt engineer(ing)?\b", r"\bmlops\b", r"\bai engineer(ing)?\b"
    ]

    STRICT_ENTRY_LEVEL_KEYWORDS = [
        r"\bjunior\b", r"\bjr\.?\b", r"\btrainee\b", r"\bintern(ship)?\b",
        r"\bestágio\b", r"\bestagio\b", r"\bestagiário\b", r"\bestagiario\b",
        r"\bgraduate\b", r"\bentry[- ]level\b", r"\bearly career\b", r"\bstudent\b",
        r"\bbolseiro\b", r"\bassociate\b"
    ]

    EXCLUDE_KEYWORDS = [
        r"\bdeloitte\b", r"\bsenior\b", r"\bsr\.?\b", r"\blead\b", r"\bprincipal\b",
        r"\bstaff\b", r"\bdirector\b", r"\bhead of\b", r"\bvp\b", r"\bmanager\b",
        r"\bgerente\b", r"\bmid[- ]level\b", r"\bpleno\b", r"\bhandyperson\b", r"\bhousekeeper\b",
        r"\bhuman resources\b", r"\bnurse\b"
    ]

    @classmethod
    def is_ai_related(cls, text: str) -> bool:
        text_lower = text.lower()
        for pattern in cls.AI_KEYWORDS:
            if re.search(pattern, text_lower):
                return True
        return False

    @classmethod
    def is_strictly_entry_level(cls, title: str, seniority_tag: str) -> bool:
        title_lower = title.lower()
        tag_lower = seniority_tag.lower() if seniority_tag else ""
        
        # 1. Verifica se o título contém explicitamente o termo
        for pattern in cls.STRICT_ENTRY_LEVEL_KEYWORDS:
            if re.search(pattern, title_lower):
                return True
                
        # 2. Ou se a plataforma categorizou explicitamente a tag
        if any(k in tag_lower for k in ["junior", "intern", "trainee", "entry"]):
            return True

        return False

    @classmethod
    def is_excluded(cls, title: str, company: str) -> bool:
        title_lower = title.lower()
        comp_lower = company.lower()
        
        # Excluir Deloitte
        if "deloitte" in comp_lower or "deloitte" in title_lower:
            return True

        # Exclusões de senioridade / irrelevantes
        for pattern in cls.EXCLUDE_KEYWORDS:
            if re.search(pattern, title_lower):
                return True
        return False

    @classmethod
    def filter_job(cls, job: JobPost) -> Optional[JobPost]:
        title_text = job.title.strip()

        # 1. Exclusões (Deloitte, Senior, Não-Tech)
        if cls.is_excluded(title_text, job.company):
            return None

        # 2. OBRIGATÓRIO: Tem de ser Junior / Trainee / Internship / Entry
        if not cls.is_strictly_entry_level(title_text, job.seniority):
            return None

        # 3. OBRIGATÓRIO: Tem de ser IA / Machine Learning / Data Science / GenAI
        full_text = f"{job.title} {job.description_snippet or ''} {' '.join(job.tags)}"
        if not cls.is_ai_related(full_text):
            return None

        job.seniority = "Junior / Trainee / Internship"
        return job
