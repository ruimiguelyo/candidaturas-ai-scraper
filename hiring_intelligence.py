import asyncio
import json
import logging
import os
import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models import JobPost

logger = logging.getLogger("HiringIntelligence")

DEFAULT_CANDIDATE_PROFILE_FILE = os.path.abspath("candidate_profile.local.json")


def is_hiring_intelligence_enabled() -> bool:
    """Verifica se a pesquisa privada foi ativada explicitamente (default: false)."""
    val = os.getenv("HIRING_INTELLIGENCE_ENABLED", "false").strip().lower()
    return val in ("true", "1", "yes", "on")


def get_max_hiring_lookups() -> int:
    """Limite de pesquisas por execução (default: 10)."""
    try:
        value = int(os.getenv("MAX_HIRING_LOOKUPS", "10").strip())
    except (ValueError, TypeError):
        return 10
    return max(0, min(value, 50))


def empty_outreach() -> Dict[str, Any]:
    """Retorna o esquema de outreach padrão quando nenhum target é encontrado."""
    return {
        "target_found": False,
        "target_type": "NONE",
        "name": None,
        "current_title": None,
        "company": None,
        "profile_url": None,
        "confidence": "NONE",
        "verification_status": "NOT_FOUND",
        "outreach_recommendation": "NO",
        "evidence": [],
        "personalization_hook": None,
        "candidate_proof": None,
        "suggested_message": None,
    }


class HiringIntelligence:
    """
    Camada opcional de pós-processamento para identificação de Hiring Managers e Recruiters
    através de pesquisa pública sem custos (100% gratuita, €0).
    """

    COMPANY_SUFFIXES = {
        "and",
        "company",
        "corp",
        "corporation",
        "group",
        "inc",
        "international",
        "limited",
        "llc",
        "ltd",
        "portugal",
        "sa",
    }

    @staticmethod
    def _normalize_text(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", str(value or ""))
        without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
        return " ".join(re.findall(r"[a-z0-9]+", without_accents.casefold().replace("&", " and ")))

    @classmethod
    def _result_mentions_company(cls, parsed: Dict[str, Any], company: str) -> bool:
        expected_tokens = [
            token
            for token in cls._normalize_text(company).split()
            if token not in cls.COMPANY_SUFFIXES
        ]
        if not expected_tokens:
            return False
        result_tokens = set(
            cls._normalize_text(
                " ".join(
                    str(parsed.get(field, ""))
                    for field in ("search_title", "current_title", "snippet")
                )
            ).split()
        )
        return set(expected_tokens).issubset(result_tokens)

    @classmethod
    def load_candidate_profile(cls) -> Optional[Dict[str, Any]]:
        """Carrega um perfil local/privado, nunca o exportando para os datasets."""
        profile_path = os.getenv("CANDIDATE_PROFILE_FILE", DEFAULT_CANDIDATE_PROFILE_FILE)
        if not os.path.exists(profile_path):
            logger.warning(
                "Perfil privado do candidato não encontrado em %s. Outreach ignorado.",
                profile_path,
            )
            return None
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Erro ao carregar o perfil privado do candidato: %s", e)
            return None

    @classmethod
    def extract_job_domain(cls, job: JobPost) -> Tuple[str, List[str]]:
        """Extrai a área técnica, tecnologias e foco funcional da vaga."""
        text = f"{job.title} {job.description_snippet or ''} {' '.join(job.tags)}".lower()

        # Deteção de equipa / área
        if any(k in text for k in ["computer vision", "visão computacional", "image", "video"]):
            domain = "Computer Vision"
        elif any(k in text for k in ["nlp", "natural language", "llm", "rag", "genai", "generative ai"]):
            domain = "AI & GenAI Engineering"
        elif any(k in text for k in ["machine learning", "mlops", "deep learning"]):
            domain = "Machine Learning"
        elif any(k in text for k in ["data science", "data scientist", "data engineer"]):
            domain = "Data & AI"
        elif any(k in text for k in ["backend", "back-end", "systems", "cloud", "infrastructure", "devops"]):
            domain = "Software & Systems Engineering"
        else:
            domain = "Engineering"

        # Extração de tecnologias mencionadas
        tech_keywords = [
            "Python", "PyTorch", "TensorFlow", "FastAPI", "Docker", "Kubernetes",
            "LangChain", "RAG", "LLMs", "AWS", "GCP", "Azure", "SQL", "CI/CD"
        ]
        matched_tech = [t for t in tech_keywords if re.search(rf"\b{re.escape(t.lower())}\b", text)]
        return domain, matched_tech

    @classmethod
    def search_public_web(cls, query: str, max_results: int = 4) -> List[Dict[str, str]]:
        """
        Executa pesquisa pública gratuita via ddgs (DuckDuckGo open search).
        Custo: €0. Sem APIs pagas, sem cartão, sem billing.
        """
        results: List[Dict[str, str]] = []
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
                for item in raw:
                    title = item.get("title", "")
                    href = item.get("href", "")
                    body = item.get("body", "")
                    if title and href:
                        results.append({"title": title, "link": href, "snippet": body})
        except Exception as exc:
            logger.debug("Pesquisa pública ddgs para '%s' falhou ou sem resultados: %s", query, exc)
        return results

    @classmethod
    def _parse_linkedin_result(cls, result: Dict[str, str], company: str) -> Optional[Dict[str, Any]]:
        """Extrai nome, cargo atual e perfil LinkedIn a partir de um snippet de pesquisa indexado."""
        raw_title = result.get("title", "")
        snippet = result.get("snippet", "")
        link = result.get("link", "")

        # Padrão LinkedIn: "Nome da Pessoa - Cargo - Nome da Empresa | LinkedIn" ou "Nome - Cargo | LinkedIn"
        clean_title = re.sub(r"\s*\|\s*LinkedIn.*$", "", raw_title, flags=re.IGNORECASE).strip()
        parts = [p.strip() for p in clean_title.split("-") if p.strip()]

        if len(parts) >= 2:
            name = parts[0]
            current_title = parts[1]
        elif len(parts) == 1:
            name = parts[0]
            current_title = ""
        else:
            return None

        # Validação de nome válido (excluir títulos genéricos)
        if len(name.split()) > 5 or any(w in name.lower() for w in ["jobs", "vagas", "careers", "linkedin", "overview"]):
            return None

        # Aceitar apenas um perfil pessoal real do LinkedIn. A implementação
        # anterior devolvia o URL original mesmo quando era de outro domínio.
        try:
            parsed_url = urllib.parse.urlparse(link)
        except ValueError:
            return None
        hostname = (parsed_url.hostname or "").casefold()
        if not (hostname == "linkedin.com" or hostname.endswith(".linkedin.com")):
            return None
        profile_path = parsed_url.path.rstrip("/")
        if not re.fullmatch(r"/in/[^/?#]+", profile_path):
            return None
        clean_url = urllib.parse.urlunsplit(("https", hostname, profile_path, "", ""))

        return {
            "name": name,
            "current_title": current_title,
            "snippet": snippet,
            "profile_url": clean_url,
            "search_title": raw_title,
        }

    @classmethod
    def find_target(cls, job: JobPost, domain: str) -> Tuple[str, Optional[Dict[str, Any]], str, List[str]]:
        """
        Executa a pesquisa hierárquica (Hiring Manager primeiro, Recruiter como fallback).
        Retorna: (target_type, target_dict, confidence, evidence_list)
        """
        company_clean = job.company.strip()
        evidence: List[str] = []

        # 1. TENTATIVA 1: Pesquisa por Hiring Manager / Engineering Manager da área
        hm_queries = [
            f'site:linkedin.com/in "{company_clean}" "{domain}" Manager',
            f'site:linkedin.com/in "{company_clean}" "Engineering Manager"',
            f'site:linkedin.com/in "{company_clean}" "Machine Learning Manager"',
            f'site:linkedin.com/in "{company_clean}" "AI Lead"'
        ]

        for q in hm_queries:
            results = cls.search_public_web(q, max_results=3)
            for r in results:
                parsed = cls._parse_linkedin_result(r, company_clean)
                if not parsed:
                    continue

                if not cls._result_mentions_company(parsed, company_clean):
                    continue

                # A função de gestão tem de estar no título atual. Palavras soltas
                # no snippet podem descrever outra pessoa ou uma vaga e não servem
                # para atribuir um cargo ao resultado.
                title_lower = parsed["current_title"].lower()
                context_lower = f"{parsed['current_title']} {parsed['snippet']}".lower()

                # Verifica se a pessoa tem cargo relevante de gestão de engenharia
                is_manager = any(m in title_lower for m in [
                    "engineering manager", "software engineering manager", "ml manager",
                    "machine learning manager", "ai manager", "head of ai", "head of data",
                    "lead ai", "lead machine learning", "ai lead", "tech lead", "engineering lead"
                ])

                # Rejeitar executivos demasiado distantes a menos que haja sinal direto
                is_distant_exec = any(e in title_lower for e in ["cto", "vp of engineering", "chief technology officer"])

                if is_manager and not is_distant_exec:
                    # Avaliação de Confiança
                    signals = 0
                    if domain.lower() in context_lower:
                        signals += 2
                        evidence.append(f"O resultado público menciona a área {domain}.")
                    if any(loc in context_lower for loc in ["portugal", "lisbon", "porto", "remote"]):
                        signals += 1
                        evidence.append("O resultado público menciona uma localização compatível.")
                    if any(w in context_lower for w in ["hiring", "team", "lead", "building", "engineer"]):
                        signals += 1
                        evidence.append("O resultado público contém sinais de liderança técnica.")

                    if signals >= 2:
                        confidence = "HIGH"
                    else:
                        confidence = "MEDIUM"

                    evidence.append(
                        f"O título/snippet da pesquisa menciona {parsed['current_title']} e {company_clean}; confirmar no perfil."
                    )
                    return "HIRING_MANAGER", parsed, confidence, evidence

        # 2. TENTATIVA 2: Fallback para Recruiter Técnico relevante
        recruiter_queries = [
            f'site:linkedin.com/in "{company_clean}" "Technical Recruiter"',
            f'site:linkedin.com/in "{company_clean}" "Talent Acquisition Partner" Engineering'
        ]

        for q in recruiter_queries:
            results = cls.search_public_web(q, max_results=3)
            for r in results:
                parsed = cls._parse_linkedin_result(r, company_clean)
                if not parsed:
                    continue

                if not cls._result_mentions_company(parsed, company_clean):
                    continue

                title_lower = parsed["current_title"].lower()
                context_lower = f"{parsed['current_title']} {parsed['snippet']}".lower()
                is_recruiter = any(rec in title_lower for rec in [
                    "technical recruiter", "tech recruiter", "talent acquisition partner",
                    "engineering recruiter", "technical talent partner"
                ])

                if is_recruiter:
                    evidence.append(
                        f"O título/snippet da pesquisa menciona recrutamento técnico e {company_clean}; confirmar no perfil."
                    )
                    if any(loc in context_lower for loc in ["portugal", "lisbon", "porto", "emea", "remote"]):
                        evidence.append("O resultado público menciona a região relevante.")
                        confidence = "HIGH"
                    else:
                        confidence = "MEDIUM"
                    return "RECRUITER", parsed, confidence, evidence

        return "NONE", None, "NONE", []

    @classmethod
    def select_candidate_proof(cls, job: JobPost, domain: str, tech_list: List[str], profile: Dict[str, Any]) -> Tuple[str, str]:
        """
        Seleciona uma prova factual do candidate_profile.json e um hook profissional.
        Regra estrita: NUNCA inventar dados que não existam no perfil.
        """
        projects = profile.get("projects", [])
        skills = profile.get("skills", [])

        # Prefer an explicit project from the user-maintained profile. The text is
        # derived only from its name and technologies; no experience or deployment
        # claim is inferred merely from a skill appearing in a list.
        job_terms = set(cls._normalize_text(f"{job.title} {domain} {' '.join(tech_list)}").split())
        ranked_projects = []
        for project in projects:
            project_terms = set(
                cls._normalize_text(
                    f"{project.get('name', '')} {project.get('description', '')} "
                    f"{' '.join(project.get('technologies', []) or [])}"
                ).split()
            )
            ranked_projects.append((len(job_terms & project_terms), project))

        if ranked_projects:
            _, project = max(ranked_projects, key=lambda item: item[0])
            project_name = str(project.get("name") or "a practical software project")
            technologies = [str(value) for value in (project.get("technologies") or []) if value]
            technology_text = ", ".join(technologies[:3])
            proof = f"building the {project_name} project"
            if technology_text:
                proof += f" with {technology_text}"
        else:
            selected_skills = [str(value) for value in skills if value]
            proof = "hands-on learning in " + ", ".join(selected_skills[:3]) if selected_skills else "practical software projects"

        hook = f"the role's focus on {domain} overlaps with my current projects and toolkit"
        return hook, proof

    @classmethod
    def generate_suggested_message(
        cls,
        name: str,
        job_title: str,
        company: str,
        target_type: str,
        hook: str,
        proof: str,
        confidence: str
    ) -> Optional[str]:
        """
        Gera uma mensagem LinkedIn concisa, profissional e direta (50-90 palavras).
        Apenas quando confidence == 'HIGH' ou 'MEDIUM'.
        """
        if confidence not in ("HIGH", "MEDIUM"):
            return None

        first_name = name.split()[0].title() if name else "there"

        # Rascunho factual: descobrir uma vaga não significa que a candidatura já
        # foi submetida. O destinatário também permanece por verificar.
        message = (
            f"Hi {first_name} — I came across the {job_title} opening at {company}.\n\n"
            f"It caught my attention because {hook}. My background includes {proof}.\n\n"
            "If you are close to this team, I would appreciate any insight into what it values "
            "in junior candidates. Thank you!"
        )
        return message

    @classmethod
    async def enrich_single_job(cls, job: JobPost, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Processa o enriquecimento de uma única vaga de forma segura e não bloqueante."""
        domain, tech_list = cls.extract_job_domain(job)

        # Executa a pesquisa pública em thread separada para não bloquear o loop de eventos
        target_type, target_data, confidence, evidence = await asyncio.to_thread(
            cls.find_target, job, domain
        )

        if not target_data or confidence == "NONE":
            return empty_outreach()

        # Decisão de Outreach
        if confidence == "HIGH":
            outreach_rec = "YES"
        elif confidence == "MEDIUM":
            outreach_rec = "VERIFY_FIRST"
        else:
            outreach_rec = "NO"

        # Selecionar Hook & Prova Factual do Candidato
        hook, proof = cls.select_candidate_proof(job, domain, tech_list, profile)

        # Gerar Mensagem Sugerida
        msg = None
        if outreach_rec in ("YES", "VERIFY_FIRST"):
            msg = cls.generate_suggested_message(
                target_data.get("name", ""),
                job.title,
                job.company,
                target_type,
                hook,
                proof,
                confidence
            )

        return {
            "target_found": True,
            "target_type": target_type,
            "name": target_data.get("name"),
            "current_title": target_data.get("current_title"),
            "company": job.company,
            "profile_url": target_data.get("profile_url"),
            "confidence": confidence,
            "verification_status": "PENDING",
            "outreach_recommendation": "VERIFY_FIRST",
            "evidence": evidence,
            "personalization_hook": hook,
            "candidate_proof": proof,
            "suggested_message": msg,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    async def enrich_jobs_async(cls, jobs: List[JobPost]) -> None:
        """
        Camada principal de enriquecimento:
        Aplica Hiring Intelligence às primeiras MAX_HIRING_LOOKUPS vagas da lista já ordenada.
        Garante Fail-Open total (nunca faz o pipeline falhar).
        """
        if not is_hiring_intelligence_enabled():
            logger.info("Hiring Intelligence está desativado (HIRING_INTELLIGENCE_ENABLED=false).")
            return

        profile = cls.load_candidate_profile()
        if not profile:
            return

        max_lookups = get_max_hiring_lookups()
        target_jobs = jobs[:max_lookups]

        logger.info("A iniciar Hiring Manager Intelligence para as primeiras %s vagas...", len(target_jobs))

        for job in target_jobs:
            try:
                job.human_outreach = await cls.enrich_single_job(job, profile)
            except Exception as exc:
                logger.warning("Falha ao enriquecer hiring intelligence para '%s': %s", job.title, exc)
                job.human_outreach = empty_outreach()
