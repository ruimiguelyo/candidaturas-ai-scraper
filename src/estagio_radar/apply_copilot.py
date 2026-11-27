"""Copiloto de candidaturas (v5.0): seleção inteligente de CV, kits personalizados, DM a recrutadores e envio por email."""

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

import httpx
import yaml

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
    r"\b(?:estagio|estágio|estágios|bolsa|investigacao|investigação|júnior|programador|analista|engenharia|informática|informatica)\b",
    re.IGNORECASE,
)
REF_CODE_RE = re.compile(
    r"((?:BI\|[0-9]{4}/[0-9]+|Ref[ªa.]?\s*[:#\-]?\s*[A-Za-z0-9./_-]{3,40}))",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IGNORED_EMAIL_FRAGMENTS = (
    "noreply",
    "no-reply",
    "donotreply",
    "privacy@",
    "gdpr@",
    "dpo@",
    "euraxess",
    "ec.europa.eu",
    "example.com",
    "sentry.io",
    "wixpress.com",
)


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


def detect_apply_strategy(job: EnrichedJob) -> dict[str, str]:
    """Classifica o melhor canal de candidatura para a vaga."""
    url_low = job.job_url.casefold()
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
    if job.source_type == "ats" or any(dom in url_low for dom in ats_domains) or "avature" in src_low:
        return {
            "type": "ats_autofill",
            "label": "ATS Direto — Auto-Preenchimento no Chrome",
            "hint": "Preenche os dados e anexa o CV PDF certo no formulário oficial da empresa.",
        }

    if "euraxess.ec.europa.eu" in url_low or EMAIL_RE.search(job.description_snippet or ""):
        return {
            "type": "email_or_portal",
            "label": "Bolsa / Candidatura por Email ou Portal",
            "hint": "Prepara o email com a referência, nota de motivação e CV em anexo.",
        }

    if "linkedin.com" in url_low:
        return {
            "type": "linkedin_portal",
            "label": "LinkedIn — Candidatar + Abordar Recrutador",
            "hint": "Abre o anúncio no Chrome (ou Easy Apply se tiveres sessão iniciada) e copia DM para recrutadores.",
        }

    return {
        "type": "web_portal",
        "label": "Portal Web — Abrir & Tentar Auto-Preencher",
        "hint": "Abre no Chrome e tenta preencher campos standard + anexar CV automaticamente.",
    }


def _filter_valid_email(candidates: list[str]) -> str | None:
    for email in candidates:
        low = email.casefold().strip()
        if any(bad in low for bad in IGNORED_EMAIL_FRAGMENTS):
            continue
        if low.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp")):
            continue
        return email.strip()
    return None


def extract_application_email(
    job_url: str,
    fallback_text: str = "",
    *,
    fetch_page: bool = False,
) -> str | None:
    """Extrai email de candidatura do snippet ou opcionalmente da página (ex: EURAXESS)."""
    direct = _filter_valid_email(EMAIL_RE.findall(fallback_text or ""))
    if direct:
        return direct
    if not fetch_page or "linkedin.com" in job_url.casefold():
        return None
    try:
        res = httpx.get(
            job_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            follow_redirects=True,
            timeout=6.0,
        )
        if res.status_code == 200:
            found = EMAIL_RE.findall(res.text)
            return _filter_valid_email(found)
    except Exception:
        return None
    return None


def build_recruiter_search_url(company: str) -> str:
    """Gera link direto no LinkedIn para encontrar recrutadores/managers da empresa em PT."""
    q = f"{company} recruiter OR talent OR engineering manager Portugal"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(q)}"


def _is_portuguese_job(job: EnrichedJob) -> bool:
    if job.primary_source in {"ITJobs.pt", "Net-Empregos"}:
        return True
    if PT_SIGNAL_RE.search(job.title):
        return True
    if "estagio" in _norm_ascii(job.title) or "júnior" in job.title.casefold():
        return True
    return False


def _domain_highlights(track: str, *, pt: bool) -> list[str]:
    if track == "ai_ml":
        if pt:
            return [
                "JudgeShift CX: pipeline tipado em Python (PyTorch, Transformers, mypy --strict) + dashboard React a avaliar LLMs com 97,2% de acordo sob stress tests.",
                "JAX Language Model Lab: Transformer decoder-only de 3,32M parâmetros criado de raiz em JAX/Flax com continual pretraining e replay.",
                "Base sólida na Deloitte Portugal (início com Python, Java e PyTorch, seguido de engenharia enterprise SAP ABAP) + Licenciatura em Engenharia Informática no IPS (média 17/20, 144 ECTS).",
            ]
        return [
            "JudgeShift CX: typed Python LLM evaluation pipeline (PyTorch, Hugging Face, mypy --strict) + React dashboard retaining 97.2% agreement under verbosity stress tests.",
            "JAX Language Model Lab: 3.32M-parameter Transformer implemented from scratch in JAX/Flax with jit-compiled Optax training and continual-pretraining replay experiments.",
            "4.5 years at Deloitte Portugal (started in Python, Java & PyTorch before transitioning to SAP ABAP) + BSc in Computer Engineering at IPS (17/20 GPA, 144 ECTS).",
        ]
    if track == "enterprise":
        if pt:
            return [
                "4,5 anos full-time na Deloitte Portugal: início em Python, Java e PyTorch, evoluindo para desenvolvimento SAP ABAP e consultoria funcional SD/MM e FI/CO (RCA, UAT e integração).",
                "Estágio Radar PT: pipeline assíncrono em Python (75+ APIs ATS diretas, SQLite, daemon 24/7, CI/CD e 33+ testes pytest).",
                "Licenciatura em Engenharia Informática no IPS (média 17/20, 144 ECTS) com projetos modernos em TypeScript/React (SplitFX) e IA aplicada (JudgeShift CX).",
            ]
        return [
            "4.5 years at Deloitte Portugal: early enterprise/ML work in Python, Java & PyTorch followed by SAP ABAP engineering and SD/MM & FI/CO consulting (RCA, UAT).",
            "Estágio Radar PT: async Python ingestion pipeline across 75+ direct ATS boards with ETag 304 caching, SQLite persistence, and 33+ automated pytest tests.",
            "BSc in Computer Engineering at IPS (17/20 GPA, 144 ECTS) combining enterprise delivery rigor with full-stack TypeScript/React (SplitFX) and Applied AI (JudgeShift CX).",
        ]
    # swe
    if pt:
        return [
            "Digital Listing Workflow: aplicação full-stack Node.js/TypeScript + PostgreSQL + Prisma ORM + OAuth 2.0 + RBAC com agente local Windows resiliente a falhas.",
            "SplitFX: aplicação React 19 / TypeScript / Redux Toolkit com aritmética determinística, Jest, Cypress E2E e pipeline CI/CD.",
            "Deloitte Portugal (4,5 anos, início em Python, Java e PyTorch e evolução para SAP ABAP, RCA e UAT) + Engenharia Informática no IPS (média 17/20).",
        ]
    return [
        "Digital Listing Workflow: full-stack Node.js/TypeScript web platform + Windows local automation agent backed by PostgreSQL, Prisma ORM, OAuth 2.0, RBAC & Vitest.",
        "SplitFX: accessible React 19 / TypeScript / Redux Toolkit app with deterministic integer arithmetic, Jest + RTL coverage, and Cypress E2E GitHub Actions CI/CD.",
        "Deloitte Portugal (4.5 yrs — early Python, Java & PyTorch before SAP ABAP delivery/RCA) + BSc Computer Engineering at IPS (17/20 GPA, 144 ECTS).",
    ]


def generate_application_kit(
    job: EnrichedJob,
    profile: dict[str, Any],
    *,
    override_track: str | None = None,
    fetch_email: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Gera kit completo pronto a usar (CV selecionado, nota, email, DM e estratégia)."""
    full_name = str(profile.get("full_name") or "Candidate")
    first_name = str(profile.get("first_name") or full_name.split()[0])
    email = str(profile.get("email") or "")
    phone = str(profile.get("phone") or "")
    github = str(profile.get("github_url") or "")
    linkedin = str(profile.get("linkedin_url") or "")

    cv_info = select_best_cv(job, profile, override_track=override_track, repo_root=repo_root)
    track = cv_info["track"]
    pt = _is_portuguese_job(job)
    strategy = detect_apply_strategy(job)
    detected_email = extract_application_email(
        job.job_url,
        f"{job.title} {job.description_snippet or ''}",
        fetch_page=fetch_email,
    )

    ref_match = REF_CODE_RE.search(job.title)
    ref_tag = f" ({ref_match.group(1).strip()})" if ref_match else ""
    highlights = _domain_highlights(track, pt=pt)

    if pt:
        if track == "ai_ml":
            proj_line = "os projetos JudgeShift CX (avaliação de LLMs em Python/PyTorch) e JAX Language Model Lab (Transformer criado de raiz em JAX/Flax)"
        elif track == "enterprise":
            proj_line = "experiência real na Deloitte (início em Python/Java/PyTorch e 4+ anos em SAP ABAP SD/MM & FI/CO) além do pipeline Estágio Radar PT em Python"
        else:
            proj_line = "projetos full-stack e backend em TypeScript/Node.js/React (Digital Listing Workflow, SplitFX) e pipelines assíncronos em Python (Estágio Radar PT)"

        short_cover_note = (
            f"Olá equipa da {job.company}, candidatei-me à vaga '{job.title}' por alinhar diretamente com o meu percurso atual. "
            f"Frequento a Licenciatura em Engenharia Informática no IPS (média atual 17/20, 144 ECTS) após 4,5 anos de experiência profissional na Deloitte Portugal "
            f"(onde comecei com Python, Java e PyTorch antes de atuar em engenharia enterprise SAP ABAP), combinando rigor de produção com {proj_line}. "
            f"Estou disponível de imediato em Setúbal/Lisboa e com enorme motivação para contribuir desde o primeiro dia."
        )
        email_subject = f"Candidatura: {job.title}{ref_tag} — {full_name}"
        email_body = (
            f"Exmos.(as) Senhores(as) da equipa {job.company},\n\n"
            f"Venho por este meio apresentar a minha candidatura à oportunidade '{job.title}'{ref_tag}.\n\n"
            f"Encontro-me a concluir a Licenciatura em Engenharia Informática no Instituto Politécnico de Setúbal (média atual 17/20, 144 ECTS), "
            f"após 4,5 anos de experiência profissional na Deloitte Portugal (com início em Python, Java e PyTorch, seguido de desenvolvimento enterprise SAP ABAP).\n\n"
            f"Destaques relevantes para esta vaga:\n"
            + "\n".join(f"- {h}" for h in highlights)
            + f"\n\nEnvio o meu CV em anexo e disponibilizo o meu GitHub ({github}) e LinkedIn ({linkedin}).\n"
            f"Agradeço desde já a atenção e fico inteiramente disponível para entrevista.\n\n"
            f"Com os melhores cumprimentos,\n{full_name}\n{phone} | {email}"
        )
        recruiter_dm = (
            f"Olá! Acabei de me candidatar à vaga '{job.title}' na {job.company}. "
            f"Sou estudante de Eng. Informática no IPS (média 17/20) em transição após 4,5 anos na Deloitte, com projetos reais em Python/IA e TypeScript no GitHub ({github}). "
            f"Se fizer sentido uma breve conversa, terei todo o gosto em partilhar mais detalhes!"
        )
    else:
        if track == "ai_ml":
            proj_line = "building reproducible AI/ML systems such as JudgeShift CX (PyTorch/Transformers LLM evaluation) and JAX Language Model Lab (from-scratch Transformer in JAX/Flax)"
        elif track == "enterprise":
            proj_line = "enterprise delivery at Deloitte Portugal (early Python, Java & PyTorch followed by SAP ABAP SD/MM & FI/CO) alongside async Python automation (Estágio Radar PT)"
        else:
            proj_line = "shipping tested full-stack and backend systems in TypeScript, Node.js, React, PostgreSQL (Digital Listing Workflow, SplitFX) and async Python (Estágio Radar PT)"

        short_cover_note = (
            f"Hi {job.company} team, I am excited to apply for '{job.title}'. "
            f"I am a BSc Computer Engineering student at IPS (17/20 GPA, 144 ECTS, graduating 2027) pivoting from 4.5 years of enterprise software engineering at Deloitte Portugal "
            f"(starting with Python, Java, and PyTorch before moving into SAP ABAP delivery) while {proj_line}. "
            f"Based in Setúbal/Lisbon with full EU work authorization, I would love to bring production discipline and strong ownership to {job.company}."
        )
        email_subject = f"Application: {job.title}{ref_tag} — {full_name}"
        email_body = (
            f"Dear {job.company} Hiring Team,\n\n"
            f"I am writing to apply for '{job.title}'{ref_tag}.\n\n"
            f"I am currently completing my BSc in Computer Engineering at the Polytechnic Institute of Setúbal (17/20 GPA, 144 ECTS) "
            f"after 4.5 years of full-time software engineering at Deloitte Portugal (starting in Python, Java, and PyTorch before working in SAP ABAP enterprise systems).\n\n"
            f"Key highlights aligned with this role:\n"
            + "\n".join(f"- {h}" for h in highlights)
            + f"\n\nMy tailored CV is attached, and my portfolio/code is available at {github} and {linkedin}.\n"
            f"Thank you for your time and consideration.\n\n"
            f"Best regards,\n{full_name}\n{phone} | {email}"
        )
        recruiter_dm = (
            f"Hi! I just applied for '{job.title}' at {job.company}. "
            f"I'm a BSc Computer Engineering student (17/20 GPA) transitioning from 4.5 yrs at Deloitte Portugal, with recent PyTorch/JAX & TypeScript projects on GitHub ({github}). "
            f"Would be glad to connect if you're reviewing candidates for the team!"
        )

    mailto_target = detected_email or ""
    mailto_url = f"mailto:{mailto_target}?subject={quote(email_subject)}&body={quote(email_body)}"

    return {
        "fingerprint": job.fingerprint,
        "company": job.company,
        "title": job.title,
        "job_url": job.job_url,
        "language": "pt" if pt else "en",
        "cv": cv_info,
        "available_cvs": list_available_cvs(profile, repo_root=repo_root),
        "strategy": strategy,
        "highlight_bullets": highlights,
        "short_cover_note": short_cover_note,
        "email_subject": email_subject,
        "email_body": email_body,
        "recruiter_dm": recruiter_dm,
        "recruiter_search_url": build_recruiter_search_url(job.company),
        "detected_email": detected_email,
        "mailto_url": mailto_url,
        "candidate": {
            "full_name": full_name,
            "first_name": first_name,
            "email": email,
            "phone": phone,
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
        return False, "SMTP_USER / SMTP_PASS não estão configurados no .env — usa o botão 'Abrir no Email' ou configura uma App Password Gmail."

    if not attachment_path.exists():
        return False, f"CV PDF não encontrado em '{attachment_path}'."

    full_name = str(profile.get("full_name") or "Rui Sá")
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
