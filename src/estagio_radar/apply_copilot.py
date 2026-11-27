"""Copiloto de candidaturas (v5.1): descrição real, redação natural com Gemini, CV certo e envio por email."""

from __future__ import annotations

import logging
import os
import re
import smtplib
import unicodedata
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from estagio_radar.ai_writer import (
    extract_tldr_bullets,
    generate_copy_with_gemini,
    load_cv_markdown,
    parse_natural_role_phrase,
)
from estagio_radar.job_details import _filter_email, fetch_job_details
from estagio_radar.models import DomainCategory, EnrichedJob

logger = logging.getLogger("estagio_radar.apply_copilot")

DEFAULT_PROFILE_PATH = Path("config/candidate_profile.yaml")

CV_TRACK_LABELS = {
    "ai_ml": "Applied AI & ML (PDF)",
    "swe": "Software Engineering Full-Stack (PDF)",
    "enterprise": "Enterprise Systems + AI (SAP/Python/Java PDF)",
}

SAP_ENTERPRISE_RE = re.compile(r"\b(?:sap|abap|erp|s/4hana|fi/co|sd/mm|mm/srm)\b", re.IGNORECASE)
PT_SIGNAL_RE = re.compile(
    r"\b(?:estagio|estágio|estágios|bolsa|investigacao|investigação|júnior|programador|analista|engenharia|informática|informatica|dados)\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def extract_application_email(
    job_url: str,
    fallback_text: str = "",
    *,
    fetch_page: bool = False,
) -> str | None:
    """Compatível com v5.0: extrai email do texto ou página oficial."""
    direct = _filter_email(fallback_text or "")
    if direct:
        return direct
    if not fetch_page or "linkedin.com" in (job_url or "").casefold():
        return None
    from estagio_radar.models import CompanyProfile, GeoZone, RoleTier, ScoreBreakdown

    dummy = EnrichedJob(
        fingerprint="tmp",
        title="",
        company="",
        location="Portugal",
        job_url=job_url,
        canonical_url=job_url,
        sources=["web"],
        primary_source="web",
        role_tier=RoleTier.INTERNSHIP,
        domain=DomainCategory.SWE,
        geo_zone=GeoZone.PT_UNSPECIFIED,
        description_snippet=fallback_text,
        company_profile=CompanyProfile(company_name="x", normalized_name="x"),
        score=50.0,
        score_breakdown=ScoreBreakdown(),
    )
    return fetch_job_details(dummy).get("detected_email")


def _norm_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def load_candidate_profile(path: str | Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    """Carrega o perfil pessoal em config/candidate_profile.yaml."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as err:
        logger.warning("Erro ao ler perfil %s: %s", p, err)
        return {}


def _resolve_cv_path(raw_path: str, repo_root: Path) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else (repo_root / p)


def list_available_cvs(
    profile: dict[str, Any],
    *,
    repo_root: Path | None = None,
) -> list[dict[str, Any]]:
    root = repo_root or Path.cwd()
    cvs_cfg = profile.get("cvs") or {}
    out: list[dict[str, Any]] = []
    for track in ("ai_ml", "swe", "enterprise"):
        raw = str(cvs_cfg.get(track) or "").strip()
        if not raw:
            continue
        resolved = _resolve_cv_path(raw, root)
        out.append(
            {
                "track": track,
                "label": CV_TRACK_LABELS[track],
                "path": str(resolved),
                "filename": resolved.name,
                "exists": resolved.exists(),
            }
        )
    return out


def select_best_cv(
    job: EnrichedJob,
    profile: dict[str, Any],
    *,
    override_track: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Escolhe automaticamente o CV PDF mais adequado à área e stack da vaga."""
    root = repo_root or Path.cwd()
    cvs_cfg = profile.get("cvs") or {}

    if override_track in {"ai_ml", "swe", "enterprise"} and cvs_cfg.get(override_track):
        track = override_track
        reason = "Selecionado manualmente"
    elif SAP_ENTERPRISE_RE.search(f"{job.title} {' '.join(job.tags)}"):
        track = "enterprise" if cvs_cfg.get("enterprise") else "swe"
        reason = "Vaga com foco SAP/Enterprise — CV Enterprise + Engenharia"
    elif job.domain in {DomainCategory.AI_ML, DomainCategory.DATA}:
        track = "ai_ml"
        reason = f"Área {job.domain.value} — destaca JudgeShift CX, JAX Lab e Estágio Radar PT"
    else:
        track = "swe" if cvs_cfg.get("swe") else "ai_ml"
        reason = f"Área {job.domain.value} — destaca Digital Listing Workflow, SplitFX e Estágio Radar PT"

    raw_path = str(cvs_cfg.get(track) or cvs_cfg.get("ai_ml") or cvs_cfg.get("swe") or "")
    resolved = _resolve_cv_path(raw_path, root) if raw_path else Path("")
    return {
        "track": track,
        "label": CV_TRACK_LABELS.get(track, track),
        "path": str(resolved) if raw_path else "",
        "filename": resolved.name if raw_path else "",
        "exists": bool(raw_path and resolved.exists()),
        "reason": reason,
    }


def detect_apply_strategy(job: EnrichedJob, external_apply_url: str | None = None) -> dict[str, str]:
    """Classifica o melhor canal de candidatura para a vaga."""
    effective_url = (external_apply_url or job.job_url or "").casefold()
    src_low = job.primary_source.casefold()

    ats_domains = (
        "greenhouse.io",
        "ashbyhq.com",
        "lever.co",
        "smartrecruiters.com",
        "recruitee.com",
        "teamtailor.com",
        "careers.tekever.com",
        "careers.coverflex.com",
        "careers.infraspeak.com",
        "careers.stratioautomotive.com",
        "jobs.siemens.com",
        "myworkdayjobs.com",
        "jobs.edp.com",
        "careers.miniclip.com",
        "jobs.galp.com",
        "oraclecloud.com",
    )
    if external_apply_url and "linkedin.com" not in effective_url:
        return {
            "type": "ats_autofill",
            "label": "ATS / Portal Externo Direto (Sem login LinkedIn!)",
            "hint": "Link oficial extraído do anúncio — abre direto o formulário da empresa e pré-preenche.",
        }
    if job.source_type == "ats" or any(dom in effective_url for dom in ats_domains) or "avature" in src_low:
        return {
            "type": "ats_autofill",
            "label": "ATS Direto — Auto-Preenchimento no Chrome",
            "hint": "Preenche os dados, dropdowns, perguntas e anexa o CV PDF certo no formulário oficial.",
        }

    if "euraxess.ec.europa.eu" in effective_url or EMAIL_RE.search(job.description_snippet or ""):
        return {
            "type": "email_or_portal",
            "label": "Bolsa / Candidatura por Email ou Portal",
            "hint": "Prepara o email com a referência, nota de motivação natural e CV em anexo.",
        }

    if "linkedin.com" in effective_url:
        return {
            "type": "linkedin_portal",
            "label": "LinkedIn — Chrome Dedicado + Abordar Recrutador",
            "hint": "Abre no Chrome dedicado do Radar (sessão isolada dos teus outros perfis), preenche Easy Apply/ATS e anexa o CV automaticamente.",
        }

    return {
        "type": "web_portal",
        "label": "Portal Web — Abrir & Auto-Preencher",
        "hint": "Abre no Chrome e pré-preenche campos standard + anexa CV automaticamente.",
    }


def build_recruiter_search_url(company: str) -> str:
    """Gera link direto no LinkedIn para encontrar recrutadores/managers da empresa em PT."""
    q = f"{company} recruiter OR talent OR engineering manager Portugal"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(q)}"


def _is_portuguese_job(job: EnrichedJob, description: str = "") -> bool:
    if job.primary_source in {"ITJobs.pt", "Net-Empregos"}:
        return True
    if PT_SIGNAL_RE.search(job.title):
        return True
    if "estagio" in _norm_ascii(job.title) or "júnior" in job.title.casefold():
        return True
    if description and re.search(r"\b(?:responsabilidades|requisitos|candidatura|procuramos|estágio)\b", description, re.I):
        return True
    return False


def _domain_highlights(track: str, *, pt: bool) -> list[str]:
    if track == "ai_ml":
        if pt:
            return [
                "JudgeShift CX: pipeline tipado em Python (PyTorch, Transformers, mypy --strict) + dashboard React para avaliação de LLMs com 97,2% de consistência.",
                "JAX Language Model Lab: Transformer de 3,32M parâmetros criado de raiz em JAX/Flax com continual pretraining e replay.",
                "Deloitte Portugal (4,5 anos — início em Python, Java e PyTorch, seguido de SAP ABAP) + Licenciatura em Eng. Informática no IPS (17/20, 144 ECTS).",
            ]
        return [
            "JudgeShift CX: typed Python LLM evaluation pipeline (PyTorch, Hugging Face, mypy --strict) + React dashboard retaining 97.2% agreement under stress tests.",
            "JAX Language Model Lab: 3.32M-parameter Transformer implemented from scratch in JAX/Flax with jit-compiled Optax training.",
            "4.5 years at Deloitte Portugal (started in Python, Java & PyTorch before SAP ABAP) + BSc Computer Engineering at IPS (17/20 GPA, 144 ECTS).",
        ]
    if track == "enterprise":
        if pt:
            return [
                "Deloitte Portugal (4,5 anos): início com Python, Java e PyTorch, evoluindo para desenvolvimento SAP ABAP e consultoria funcional SD/MM & FI/CO (RCA e UAT).",
                "Estágio Radar PT: pipeline assíncrono em Python (75+ APIs ATS diretas, SQLite, daemon 24/7, CI/CD e 38 testes pytest).",
                "Engenharia Informática no IPS (17/20, 144 ECTS) com projetos modernos em TypeScript/React (SplitFX) e IA aplicada (JudgeShift CX).",
            ]
        return [
            "Deloitte Portugal (4.5 yrs): early Python, Java & PyTorch followed by SAP ABAP engineering and SD/MM & FI/CO functional consulting (RCA, UAT).",
            "Estágio Radar PT: async Python ingestion pipeline across 75+ direct ATS boards with ETag 304 caching, SQLite, and 38 automated tests.",
            "BSc Computer Engineering at IPS (17/20 GPA, 144 ECTS) bridging enterprise rigor with TypeScript/React (SplitFX) and Applied AI (JudgeShift CX).",
        ]
    if pt:
        return [
            "Digital Listing Workflow: plataforma full-stack Node.js/TypeScript + PostgreSQL + Prisma ORM + OAuth 2.0 + RBAC e agente local Windows.",
            "SplitFX: aplicação React 19 / TypeScript / Redux Toolkit com aritmética determinística, Jest, Cypress E2E e CI/CD.",
            "Deloitte Portugal (4,5 anos — início em Python, Java e PyTorch antes de SAP ABAP, RCA e UAT) + Eng. Informática no IPS (média 17/20).",
        ]
    return [
        "Digital Listing Workflow: full-stack Node.js/TypeScript web platform + Windows local automation agent backed by PostgreSQL, Prisma ORM, OAuth 2.0 & Vitest.",
        "SplitFX: accessible React 19 / TypeScript / Redux Toolkit app with deterministic integer arithmetic, Jest + RTL coverage, and Cypress E2E CI/CD.",
        "Deloitte Portugal (4.5 yrs — early Python, Java & PyTorch before SAP ABAP delivery/RCA) + BSc Computer Engineering at IPS (17/20 GPA, 144 ECTS).",
    ]


def _trim_dm(text: str, limit: int = 280) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    clipped = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped + "…"


def _build_offline_copy(
    *,
    job: EnrichedJob,
    description: str,
    profile: dict[str, Any],
    track: str,
    pt: bool,
    tone: str,
) -> dict[str, Any]:
    full_name = str(profile.get("full_name") or "Candidate")
    email = str(profile.get("email") or "")
    phone = str(profile.get("phone") or "")
    github = str(profile.get("github_url") or "")
    linkedin = str(profile.get("linkedin_url") or "")

    role_info = parse_natural_role_phrase(job.title, job.company, pt=pt)
    role_label = role_info["role_label"]
    apply_phrase = role_info["apply_phrase"]
    highlights = _domain_highlights(track, pt=pt)
    tldr = extract_tldr_bullets(description, pt=pt)

    if pt:
        if track == "ai_ml":
            proj_focus = "projetos práticos em Python, PyTorch e JAX (como o JudgeShift CX para avaliação de LLMs e um Transformer criado de raiz)"
        elif track == "enterprise":
            proj_focus = "experiência real na Deloitte (início em Python, Java e PyTorch, seguido de SAP ABAP SD/MM e FI/CO) aliada a automação assíncrona em Python"
        else:
            proj_focus = "sistemas full-stack em TypeScript, Node.js, React e PostgreSQL, além de pipelines assíncronos em Python com testes automatizados"

        if tone == "concise":
            short_cover_note = (
                f"Olá equipa da {job.company}, candidato-me {apply_phrase} por encaixar diretamente no meu foco em {proj_focus}. "
                f"Estou a terminar Engenharia Informática no IPS (média 17/20, 144 ECTS) após 4,5 anos na Deloitte Portugal, e fico disponível de imediato em Setúbal/Lisboa."
            )
        else:
            short_cover_note = (
                f"Olá equipa da {job.company}, escrevo para me candidatar {apply_phrase}. "
                f"Encontro-me a concluir a Licenciatura em Engenharia Informática no IPS (média atual 17/20, 144 ECTS) depois de 4,5 anos na Deloitte Portugal, "
                f"onde comecei em desenvolvimento com Python, Java e PyTorch antes de seguir para engenharia enterprise SAP ABAP. "
                f"Hoje trago essa maturidade de produção combinada com {proj_focus}, com disponibilidade imediata para contribuir na região de Setúbal/Lisboa."
            )
        email_subject = f"Candidatura — {role_label} ({job.company}) — {full_name}"
        email_body = (
            f"Exmos.(as) Senhores(as) da equipa {job.company},\n\n"
            f"Venho apresentar a minha candidatura {apply_phrase}.\n\n"
            f"Encontro-me a concluir a Licenciatura em Engenharia Informática no Instituto Politécnico de Setúbal (média 17/20, 144 ECTS), "
            f"em transição após 4,5 anos de experiência profissional na Deloitte Portugal (início com Python, Java e PyTorch, seguido de desenvolvimento SAP ABAP).\n\n"
            f"Pontos relevantes do meu percurso:\n"
            + "\n".join(f"- {h}" for h in highlights)
            + f"\n\nEnvio o meu CV em anexo e deixo o meu GitHub ({github}) e LinkedIn ({linkedin}).\n"
            f"Fico inteiramente disponível para uma breve conversa.\n\n"
            f"Com os melhores cumprimentos,\n{full_name}\n{phone} | {email}"
        )
        recruiter_dm = _trim_dm(
            f"Olá! Candidatei-me {apply_phrase} na {job.company}. "
            f"Concluo Eng. Informática no IPS (17/20) após 4,5 anos na Deloitte, com projetos em Python/IA e TypeScript ({github}). "
            f"Podemos falar?"
        )
    else:
        if track == "ai_ml":
            proj_focus = "hands-on Python, PyTorch, and JAX engineering (JudgeShift CX LLM evaluation and a from-scratch Transformer lab)"
        elif track == "enterprise":
            proj_focus = "enterprise engineering at Deloitte (early Python, Java & PyTorch followed by SAP ABAP SD/MM & FI/CO) alongside async Python automation"
        else:
            proj_focus = "tested full-stack and backend systems in TypeScript, Node.js, React, PostgreSQL, and async Python"

        if tone == "concise":
            short_cover_note = (
                f"Hi {job.company} team, I am applying {apply_phrase} given my focus on {proj_focus}. "
                f"I am completing a BSc in Computer Engineering at IPS (17/20 GPA, 144 ECTS) after 4.5 years at Deloitte Portugal, with immediate availability in Lisbon/Setúbal."
            )
        else:
            short_cover_note = (
                f"Hi {job.company} team, I am writing to apply {apply_phrase}. "
                f"I am currently completing my BSc in Computer Engineering at IPS (17/20 GPA, 144 ECTS, graduating 2027) after 4.5 years at Deloitte Portugal, "
                f"where I started working with Python, Java, and PyTorch before moving into SAP ABAP delivery and incident RCA. "
                f"Today I pair that production discipline with {proj_focus}, and I am ready to contribute in Lisbon/Setúbal with full EU work authorization."
            )
        email_subject = f"Application: {role_label} ({job.company}) — {full_name}"
        email_body = (
            f"Dear {job.company} Hiring Team,\n\n"
            f"I am writing to apply {apply_phrase}.\n\n"
            f"I am completing my BSc in Computer Engineering at the Polytechnic Institute of Setúbal (17/20 GPA, 144 ECTS) "
            f"after 4.5 years of full-time engineering experience at Deloitte Portugal (starting in Python, Java, and PyTorch before transitioning to SAP ABAP).\n\n"
            f"Key highlights aligned with this opportunity:\n"
            + "\n".join(f"- {h}" for h in highlights)
            + f"\n\nMy tailored CV is attached, and my code/portfolio is available at {github} and {linkedin}.\n"
            f"Thank you for your consideration.\n\n"
            f"Best regards,\n{full_name}\n{phone} | {email}"
        )
        recruiter_dm = _trim_dm(
            f"Hi! I applied {apply_phrase} at {job.company}. "
            f"BSc CS student (17/20 GPA) pivoting from 4.5 yrs at Deloitte, with PyTorch/JAX & TypeScript projects ({github}). Glad to connect!"
        )

    return {
        "natural_role_title": role_label,
        "role_tldr": tldr,
        "highlight_bullets": highlights,
        "short_cover_note": short_cover_note,
        "email_subject": email_subject,
        "email_body": email_body,
        "recruiter_dm": recruiter_dm,
    }


def generate_application_kit(
    job: EnrichedJob,
    profile: dict[str, Any],
    *,
    override_track: str | None = None,
    fetch_email: bool = False,
    repo_root: Path | None = None,
    storage: Any = None,
    tone: str = "balanced",
    force_refresh: bool = False,
    gemini_client: Any = None,
) -> dict[str, Any]:
    """Gera kit completo com descrição real da vaga, limpeza natural ou Gemini AI e estratégia de envio."""
    cv_info = select_best_cv(job, profile, override_track=override_track, repo_root=repo_root)
    track = cv_info["track"]

    cached = storage.get_job_details(job.fingerprint) if storage is not None else {}
    description = (cached.get("full_description") or "").strip()
    external_apply_url = cached.get("external_apply_url")
    detected_email = _filter_email(f"{job.description_snippet or ''} {description}")

    if (fetch_email or force_refresh) and not description:
        fetched = fetch_job_details(job)
        description = (fetched.get("description") or job.description_snippet or "").strip()
        if fetched.get("external_apply_url"):
            external_apply_url = fetched["external_apply_url"]
        if fetched.get("detected_email"):
            detected_email = fetched["detected_email"]
        if storage is not None and (description or external_apply_url):
            storage.save_job_details(
                job.fingerprint,
                full_description=description or None,
                external_apply_url=external_apply_url,
            )
    elif not description:
        description = (job.description_snippet or "").strip()

    pt = _is_portuguese_job(job, description)
    language = "pt" if pt else "en"
    strategy = detect_apply_strategy(job, external_apply_url=external_apply_url)
    cv_md = load_cv_markdown(cv_info["path"])

    used_ai = False
    ai_model = ""
    ai_status = "Modo offline limpo (adiciona GEMINI_API_KEY para redação adaptativa ao anúncio)"

    ai_copy, ai_info = generate_copy_with_gemini(
        job=job,
        description=description,
        cv_markdown=cv_md,
        profile=profile,
        language=language,
        tone=tone,
        client=gemini_client,
    )
    if ai_copy is not None:
        used_ai = True
        ai_model = ai_info
        ai_status = f"Gerado com Gemini ({ai_info})"
        copy_fields = ai_copy.model_dump()
    else:
        if ai_info and not ai_info.startswith("GEMINI_API_KEY"):
            ai_status = ai_info
        copy_fields = _build_offline_copy(
            job=job,
            description=description,
            profile=profile,
            track=track,
            pt=pt,
            tone=tone,
        )

    mailto_target = detected_email or ""
    mailto_url = (
        f"mailto:{mailto_target}?subject={quote(copy_fields['email_subject'])}"
        f"&body={quote(copy_fields['email_body'])}"
    )

    return {
        "fingerprint": job.fingerprint,
        "company": job.company,
        "title": job.title,
        "natural_role_title": copy_fields["natural_role_title"],
        "job_url": job.job_url,
        "external_apply_url": external_apply_url,
        "language": language,
        "tone": tone,
        "used_ai": used_ai,
        "ai_model": ai_model,
        "ai_status": ai_status,
        "description_length": len(description),
        "description_preview": description[:650],
        "role_tldr": copy_fields["role_tldr"],
        "cv": cv_info,
        "available_cvs": list_available_cvs(profile, repo_root=repo_root),
        "strategy": strategy,
        "highlight_bullets": copy_fields["highlight_bullets"],
        "short_cover_note": copy_fields["short_cover_note"],
        "email_subject": copy_fields["email_subject"],
        "email_body": copy_fields["email_body"],
        "recruiter_dm": copy_fields["recruiter_dm"],
        "recruiter_search_url": build_recruiter_search_url(job.company),
        "detected_email": detected_email,
        "mailto_url": mailto_url,
        "candidate": {
            "full_name": str(profile.get("full_name") or ""),
            "first_name": str(profile.get("first_name") or ""),
            "email": str(profile.get("email") or ""),
            "phone": str(profile.get("phone") or ""),
        },
    }


def send_application_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    attachment_path: Path,
    profile: dict[str, Any],
) -> tuple[bool, str]:
    """Envia candidatura por email via SMTP com o CV PDF anexado."""
    target = (to_email or "").strip()
    if not EMAIL_RE.fullmatch(target):
        return False, "Endereço de email de destino inválido."

    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    smtp_server = (os.getenv("SMTP_SERVER") or "smtp.gmail.com").strip()
    port_raw = (os.getenv("SMTP_PORT") or "587").strip()
    smtp_port = int(port_raw) if port_raw.isdigit() else 587

    if not (smtp_user and smtp_pass):
        return False, "SMTP_USER / SMTP_PASS não estão configurados no .env — usa o botão 'Abrir na App de Email' ou configura uma App Password Gmail."

    if not attachment_path.exists():
        return False, f"CV PDF não encontrado em '{attachment_path}'."

    full_name = str(profile.get("full_name") or "Candidate")
    reply_to = str(profile.get("email") or smtp_user)

    msg = MIMEMultipart()
    msg["From"] = f"{full_name} <{smtp_user}>"
    msg["To"] = target
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with attachment_path.open("rb") as fh:
        pdf_part = MIMEApplication(fh.read(), _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename=attachment_path.name)
    msg.attach(pdf_part)

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        return True, f"Candidatura enviada para {target} com {attachment_path.name} em anexo!"
    except Exception as err:
        logger.warning("Erro SMTP ao enviar candidatura: %s", err)
        return False, f"Erro ao enviar via SMTP: {err}"
