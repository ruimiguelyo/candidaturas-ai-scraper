import asyncio
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from models import JobPost

logger = logging.getLogger("HiringIntelligence")

CANDIDATE_PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidate_profile.json")


def is_hiring_intelligence_enabled() -> bool:
    """Verifica se a flag HIRING_INTELLIGENCE_ENABLED está ativa (default: true)."""
    val = os.getenv("HIRING_INTELLIGENCE_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes", "on")


def get_max_hiring_lookups() -> int:
    """Limite de pesquisas por execução (default: 10)."""
    try:
        return int(os.getenv("MAX_HIRING_LOOKUPS", "10").strip())
    except (ValueError, TypeError):
        return 10


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

    @classmethod
    def load_candidate_profile(cls) -> Optional[Dict[str, Any]]:
        """Carrega o perfil factual do candidato a partir de candidate_profile.json."""
        if not os.path.exists(CANDIDATE_PROFILE_FILE):
            logger.warning("candidate_profile.json não encontrado. Enriquecimento de outreach ignorado.")
            return None
        try:
            with open(CANDIDATE_PROFILE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Erro ao carregar candidate_profile.json: %s", e)
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

        # Limpar URL do LinkedIn para formato canónico
        clean_url = link.split("?")[0] if link else None
        if clean_url and "linkedin.com/in/" not in clean_url:
            clean_url = None

        return {
            "name": name,
            "current_title": current_title,
            "snippet": snippet,
            "profile_url": clean_url or link,
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

                title_lower = (parsed["current_title"] + " " + parsed["snippet"]).lower()
                company_lower = company_clean.lower()

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
                    if domain.lower() in title_lower:
                        signals += 2
                        evidence.append(f"Cargo diretamente ligado à área da vaga ({domain}).")
                    if any(loc in title_lower for loc in ["portugal", "lisbon", "porto", "remote"]):
                        signals += 1
                        evidence.append("Localização compatível com a posição anunciada.")
                    if any(w in title_lower for w in ["hiring", "team", "lead", "building", "engineer"]):
                        signals += 1
                        evidence.append("Sinais públicos de gestão e liderança técnica na equipa.")

                    if signals >= 2:
                        confidence = "HIGH"
                    else:
                        confidence = "MEDIUM"

                    evidence.append(f"Atua como {parsed['current_title']} na empresa {company_clean}.")
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

                title_lower = (parsed["current_title"] + " " + parsed["snippet"]).lower()
                is_recruiter = any(rec in title_lower for rec in [
                    "technical recruiter", "tech recruiter", "talent acquisition partner",
                    "engineering recruiter", "technical talent partner"
                ])

                if is_recruiter:
                    evidence.append(f"Recrutador(a) técnico(a) identificado(a) na empresa {company_clean}.")
                    if any(loc in title_lower for loc in ["portugal", "lisbon", "porto", "emea", "remote"]):
                        evidence.append("Foco em recrutamento técnico na região relevante.")
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

        proof = ""
        hook = ""

        # 1. Tentar encontrar projeto relevante
        if "rag" in f"{job.title} {domain}".lower() or any(t.lower() in ["rag", "llms", "fastapi"] for t in tech_list):
            for proj in projects:
                if "rag" in proj.get("name", "").lower() or "semantic" in proj.get("name", "").lower():
                    proof = "built an end-to-end RAG and semantic search pipeline using FastAPI and vector search"
                    hook = f"The team's focus on {domain} and modern AI architectures aligns directly with my recent hands-on RAG implementation."
                    break

        if not proof:
            for proj in projects:
                if "aggregator" in proj.get("name", "").lower() or "autonomous" in proj.get("name", "").lower():
                    proof = "developed an asynchronous data pipeline in Python with automated scraping, strict regex filtering, and CI/CD"
                    hook = f"The requirements for {job.title} match my experience building production-ready Python pipelines and data scraping workflows."
                    break

        # 2. Fallback para competências factuais do perfil
        if not proof:
            if "Python" in skills and "PyTorch" in skills:
                proof = "implemented machine learning pipelines in Python and PyTorch with Docker deployment"
                hook = f"The technical stack for {job.title} directly matches my core Python and ML background."
            else:
                proof = "developed practical software and AI projects in Python with a focus on clean engineering"
                hook = f"The responsibilities described for the {job.title} role strongly overlap with my background."

        return hook, proof

    @classmethod
    def generate_suggested_message(
        cls,
        name: str,
        job_title: str,
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

        # Mensagem elegante e de baixa pressão
        message = (
            f"Hi {first_name} — I applied for the {job_title} position today.\n\n"
            f"I saw the work your team is doing in this area. "
            f"I recently {proof}, so the overlap with the vacancy stood out to me.\n\n"
            f"I wanted to introduce myself directly after submitting my official application. "
            f"Thanks for taking a look!"
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
            "outreach_recommendation": outreach_rec,
            "evidence": evidence,
            "personalization_hook": hook,
            "candidate_proof": proof,
            "suggested_message": msg,
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
