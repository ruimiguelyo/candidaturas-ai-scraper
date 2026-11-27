"""Redação natural em PT-PT / EN com Google Gemini API (Interactions) e limpeza inteligente offline."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.ai_writer")

DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"

GENDER_TAG_RE = re.compile(r"\(\s*(?:m/f(?:/d)?|f/m(?:/x|/d|/div\.?)?|h/m)\s*\)", re.IGNORECASE)
YEAR_NOISE_RE = re.compile(r"\b(?:202[5-9](?:\s*S\d)?)\b", re.IGNORECASE)
LOC_TRAIL_RE = re.compile(r"\s*[-–|]\s*(?:Lisbon|Lisboa|Set[úu]bal|Porto|Portugal|Remote)(?:,\s*PT)?\s*$", re.I)
GRANT_REF_RE = re.compile(r"(BI\|\d{4}/\d+)", re.IGNORECASE)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


class GeneratedApplicationCopy(BaseModel):
    """Texto estruturado natural gerado pelo Gemini para uma candidatura real."""

    natural_role_title: str = Field(description="Nome natural e limpo da função/programa, sem barras '|' nem ruído.")
    role_tldr: list[str] = Field(description="3 pontos curtos e concretos do que a vaga pede (stack, foco, requisitos).")
    short_cover_note: str = Field(description="Nota de apresentação humana em 3-4 frases naturais e diretas.")
    email_subject: str = Field(description="Assunto claro e profissional para email de candidatura.")
    email_body: str = Field(description="Corpo completo do email de candidatura em texto simples, natural e conciso.")
    recruiter_dm: str = Field(description="Mensagem curta para recrutador no LinkedIn (máximo 280 caracteres).")
    highlight_bullets: list[str] = Field(description="3 destaques reais do CV alinhados aos requisitos da vaga.")


class FormAnswersPayload(BaseModel):
    """Respostas estruturadas às perguntas abertas de um formulário ATS."""

    answers: dict[str, str] = Field(default_factory=dict, description="Mapa exato pergunta -> resposta concisa.")


def parse_natural_role_phrase(raw_title: str, company: str, *, pt: bool) -> dict[str, str]:
    """Transforma títulos crus de portais (com '|', '(m/f)', etc.) numa expressão humana natural."""
    title = re.sub(r"\s+", " ", (raw_title or "").strip())
    title = GENDER_TAG_RE.sub("", title)
    title = re.sub(r"Application submission from.*$", "", title, flags=re.IGNORECASE).strip()
    title = LOC_TRAIL_RE.sub("", title).strip()

    comp_clean = re.sub(r"\s+", " ", (company or "").strip())
    comp_short = re.sub(r"\b(?:in Portugal|Portugal|Corporation|SA|Group|The)\b", "", comp_clean, flags=re.I).strip()

    # 1. Caso específico Bolsas / EURAXESS (ex: "Public notice for one Research Grant - BI|2026/975 Projet SHELL– refª ...")
    grant_ref_m = GRANT_REF_RE.search(title)
    if re.search(r"\b(?:research\s+grant|bolsa\s+de\s+investiga[cç][aã]o|public\s+notice)\b", title, re.IGNORECASE):
        proj_m = re.search(r"Proje(?:c?t|to)\s+([A-Za-z0-9_-]{2,30})", title, re.IGNORECASE)
        ref_str = grant_ref_m.group(1) if grant_ref_m else ""
        if proj_m:
            proj = proj_m.group(1).strip()
            ref_part = f" ({ref_str})" if ref_str else ""
            if pt:
                role_label = f"Bolsa de Investigação — Projeto {proj}{ref_part}"
                apply_phrase = f"à bolsa de investigação do projeto {proj}{ref_part}"
            else:
                role_label = f"Research Fellowship — Project {proj}{ref_part}"
                apply_phrase = f"for the Project {proj} research fellowship{ref_part}"
            return {"role_label": role_label, "apply_phrase": apply_phrase, "ref_code": ref_str}

    # 2. Caso Navigator BEaPRO / Programas com "|"
    if "beapro" in title.casefold():
        area = re.sub(r"^.*?est[áa]gios?\s+profissionais\s*[-–:]?\s*", "", title, flags=re.IGNORECASE).strip(" |-–")
        if not area or "beapro" in area.casefold():
            area = "Informática e Dados" if pt else "Analytics & Tech"
        if pt:
            return {
                "role_label": f"Programa BEaPRO — {area}",
                "apply_phrase": f"ao programa BEaPRO na área de {area}",
                "ref_code": "",
            }
        return {
            "role_label": f"BEaPRO Programme — {area}",
            "apply_phrase": f"for the BEaPRO programme in {area}",
            "ref_code": "",
        }

    # 3. Remover duplicação da empresa ou prefixos genéricos após "|"
    parts = [p.strip(" -–") for p in title.split("|") if p.strip(" -–")]
    if len(parts) >= 2:
        p0, p1 = parts[0], parts[1]
        if re.search(r"\b(?:extracurricular|curricular|est[áa]gios?\s+profissionais|internship\s+program)\b", p0, re.I):
            cleaned_second = re.sub(r"^Functional Consulting\s*[-–]\s*", "", p1, flags=re.I).strip()
            clean_core = cleaned_second or p1
        elif re.search(r"\b(?:trainee\s+program|graduate\s+program(?:me)?|academy)\b", p1, re.I):
            prog = YEAR_NOISE_RE.sub("", p1).strip(" -–")
            clean_core = f"{p0} ({prog})"
        elif comp_short and comp_short.casefold() in p0.casefold() and len(parts) >= 2:
            clean_core = " — ".join(parts[1:])
        else:
            clean_core = f"{p0} — {p1}"
    else:
        clean_core = parts[0] if parts else title

    clean_core = re.sub(r"^Functional Consulting\s*[-–]\s*", "", clean_core, flags=re.I).strip()
    clean_core = re.sub(r"\(\s*\)", "", clean_core).strip(" -–|,")
    if not clean_core:
        clean_core = "Software / AI Internship"

    low_core = clean_core.casefold()
    if pt:
        if "bolsa" in low_core:
            apply_phrase = f"à {clean_core}"
        elif "programa" in low_core or "trainee" in low_core:
            apply_phrase = f"ao programa {clean_core}"
        elif "estágio" in low_core or "estagio" in low_core:
            apply_phrase = f"ao {clean_core}"
        elif "intern" in low_core:
            apply_phrase = f"à posição de {clean_core}"
        else:
            apply_phrase = f"à função de {clean_core}"
    else:
        if "programme" in low_core or "program" in low_core:
            apply_phrase = f"for the {clean_core}"
        elif "internship" in low_core and "(" not in low_core:
            apply_phrase = f"for the {clean_core}"
        else:
            apply_phrase = f"for the {clean_core} role"

    return {
        "role_label": clean_core,
        "apply_phrase": apply_phrase,
        "ref_code": grant_ref_m.group(1) if grant_ref_m else "",
    }


def load_cv_markdown(cv_pdf_path: str | Path) -> str:
    """Lê o ficheiro Markdown irmão do CV PDF escolhido para dar contexto exato ao LLM."""
    if not cv_pdf_path:
        return ""
    md_path = Path(cv_pdf_path).with_suffix(".md")
    if not md_path.exists():
        return ""
    try:
        return md_path.read_text(encoding="utf-8")[:5500]
    except OSError:
        return ""


def extract_tldr_bullets(description: str, *, pt: bool) -> list[str]:
    """Extrai até 3 pontos concretos da descrição real da vaga quando em modo offline."""
    text = re.sub(r"\s+", " ", (description or "").strip())
    if not text:
        return [
            "Anúncio sem descrição pública carregada no scraper — vê os requisitos no link oficial."
            if pt
            else "No public job description cached — review full requirements on the official posting."
        ]

    # Procurar frases ricas em requisitos / tecnologia
    sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(text) if 35 <= len(s.strip()) <= 230]
    tech_hits = [
        s for s in sentences
        if re.search(
            r"\b(?:python|sql|power\s*bi|pytorch|jax|java|typescript|react|data|analytics|ml|ai|machine\s+learning|cloud|excel|git|api|bachelor|licenciatura|master)\b",
            s,
            re.IGNORECASE,
        )
    ]
    picked = (tech_hits or sentences)[:3]
    if not picked:
        picked = [text[:210].rstrip() + "…"]
    return picked


def _build_gemini_prompt(
    *,
    job: EnrichedJob,
    description: str,
    cv_markdown: str,
    profile: dict[str, Any],
    language: str,
    tone: str,
) -> tuple[str, str]:
    lang_rule = (
        "Escreve em Português Europeu (PT-PT) autêntico e natural (NUNCA Português do Brasil: usa 'estou a concluir', 'candidato-me', 'equipa', 'telemóvel', 'projeto')."
        if language == "pt"
        else "Write in natural, crisp, British/International English as a strong junior software/AI engineer."
    )
    tone_rule = {
        "concise": "Keep the cover note ultra-concise (2-3 punchy sentences, max 70 words).",
        "technical": "Emphasize concrete technical architecture, evaluation metrics, typing/tests, and relevant stack alignment.",
        "balanced": "Keep the cover note balanced, human, warm, and direct (3-4 crisp sentences, ~80-110 words).",
    }.get(tone, "Keep the cover note balanced and natural (3-4 crisp sentences).")

    sys_inst = (
        f"You are an expert tech career copilot helping {profile.get('full_name', 'the candidate')} apply for tech internships and junior roles.\n"
        f"{lang_rule}\n"
        f"{tone_rule}\n"
        "CRITICAL RULES:\n"
        "1. NEVER copy raw scraped titles verbatim with '|' pipes, '(m/f)', '(f/m/x)', or awkward phrasing like \"vaga 'BEaPRO | Estágios Profissionais'\". Rephrase naturally (e.g., \"programa BEaPRO da The Navigator Company na área de People Analytics\").\n"
        "2. Strictly ONLY claim skills, metrics, GPA (17/20, 144 ECTS), and experiences present in the provided CV. Note honestly that at Deloitte Portugal (Oct 2021 - Mar 2026) he started with Python, Java, and PyTorch before working mainly in SAP ABAP & SD/MM/FI-CO functional consulting, and is now pivoting back into Software Engineering / Applied AI alongside his BSc at IPS.\n"
        "3. Connect 1-2 specific details from the REAL JOB DESCRIPTION to his real projects.\n"
        "4. Sound like a thoughtful human engineer writing directly — never generic AI fluff like 'I am thrilled to express my keen interest'."
    )

    user_input = (
        f"COMPANY: {job.company}\n"
        f"RAW JOB TITLE: {job.title}\n"
        f"LOCATION: {job.location} ({job.modality})\n"
        f"JOB URL: {job.job_url}\n\n"
        f"REAL JOB DESCRIPTION:\n{description[:4000] if description else '(Not provided — rely cleanly on company and role focus.)'}\n\n"
        f"CANDIDATE PROFILE: {profile.get('full_name')} | {profile.get('email')} | {profile.get('phone')} | {profile.get('location')} | GitHub: {profile.get('github_url')} | LinkedIn: {profile.get('linkedin_url')}\n\n"
        f"CANDIDATE CV:\n{cv_markdown[:4500]}\n"
    )
    return sys_inst, user_input


def generate_copy_with_gemini(
    *,
    job: EnrichedJob,
    description: str,
    cv_markdown: str,
    profile: dict[str, Any],
    language: str,
    tone: str = "balanced",
    client: Any = None,
) -> tuple[GeneratedApplicationCopy | None, str]:
    """Gera textos naturais em JSON estruturado usando Gemini Interactions API."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if client is None and not api_key:
        return None, "GEMINI_API_KEY não configurada"

    model = (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    if model.startswith(("gemini-2.5", "gemini-2.0", "gemini-1.5")):
        model = DEFAULT_GEMINI_MODEL

    try:
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)

        sys_inst, prompt_input = _build_gemini_prompt(
            job=job,
            description=description,
            cv_markdown=cv_markdown,
            profile=profile,
            language=language,
            tone=tone,
        )
        interaction = client.interactions.create(
            model=model,
            system_instruction=sys_inst,
            input=prompt_input,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": GeneratedApplicationCopy.model_json_schema(),
            },
            store=False,
        )
        raw_out = getattr(interaction, "output_text", None) or ""
        parsed = GeneratedApplicationCopy.model_validate_json(raw_out)
        return parsed, model
    except Exception as err:
        logger.warning("Fallback offline (erro Gemini): %s", err)
        return None, f"Erro Gemini ({err})"


def answer_form_questions_with_gemini(
    *,
    questions: list[str],
    company: str,
    title: str,
    page_text: str,
    cv_markdown: str,
    profile: dict[str, Any],
    language: str = "en",
    client: Any = None,
) -> dict[str, str]:
    """Responde a perguntas abertas específicas de um formulário ATS com Gemini ou regras limpas."""
    clean_qs = [q.strip() for q in questions if q and q.strip()][:8]
    if not clean_qs:
        return {}

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if client is not None or api_key:
        model = (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
        try:
            if client is None:
                from google import genai

                client = genai.Client(api_key=api_key)

            sys_inst = (
                "You fill open-ended internship/junior job application questions truthfully and concisely (2-4 sentences per answer). "
                "Use European Portuguese (PT-PT) if the question is in Portuguese, otherwise crisp English. "
                "Never invent skills not in the CV. Return a JSON object mapping each exact question string to its concise answer."
            )
            q_list = "\n".join(f"- {q}" for q in clean_qs)
            prompt = (
                f"Company: {company} | Role: {title}\n"
                f"Page Context:\n{page_text[:2500]}\n\n"
                f"Candidate Profile: Setúbal, Portugal | EU Citizen/Work Auth: {profile.get('work_authorization_eu', 'Yes')} | "
                f"Sponsorship Needed: {profile.get('requires_sponsorship', 'No')} | Availability: {profile.get('availability', 'Immediate')} | "
                f"BSc Computer Engineering IPS (17/20 GPA, 144 ECTS, 2027)\n\n"
                f"Candidate CV:\n{cv_markdown[:3500]}\n\n"
                f"Questions to answer:\n{q_list}"
            )
            interaction = client.interactions.create(
                model=model,
                system_instruction=sys_inst,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": FormAnswersPayload.model_json_schema(),
                },
                store=False,
            )
            parsed = FormAnswersPayload.model_validate_json(getattr(interaction, "output_text", "") or "{}")
            if parsed.answers:
                return parsed.answers
        except Exception as err:
            logger.warning("Fallback respostas offline (erro Gemini): %s", err)

    # Fallback determinístico seguro baseado no perfil
    role_info = parse_natural_role_phrase(title, company, pt=(language == "pt"))
    out: dict[str, str] = {}
    for q in clean_qs:
        ql = q.casefold()
        if any(k in ql for k in ("visa", "sponsor", "work author", "right to work", "autoriza", "visto")):
            out[q] = "Yes — I hold full EU work authorization in Portugal and do not require visa sponsorship."
        elif any(k in ql for k in ("availab", "start date", "when can you", "disponibilidade", "início")):
            out[q] = str(profile.get("availability") or "Immediate — available for curricular or extracurricular internship in 2026/2027.")
        elif any(k in ql for k in ("salary", "compensation", "expect", "remunera", "salário")):
            out[q] = "Open and aligned with standard internship/junior bands."
        elif any(k in ql for k in ("location", "relocat", "commute", "hybrid", "setúbal", "lisboa", "morada")):
            out[q] = "Based in Setúbal, Portugal — available for on-site/hybrid work in Setúbal or Greater Lisbon, as well as remote roles."
        elif any(k in ql for k in ("why", "motivat", "interest", "about yourself", "cover", "porquê", "motivação", "sobre ti", "apresenta")):
            if language == "pt" or any(w in ql for w in ("porqu", "motiv", "sobre", "apresent")):
                out[q] = (
                    f"Candidato-me {role_info['apply_phrase']} na {company} por querer aplicar engenharia prática em contexto real. "
                    f"Estou a concluir Engenharia Informática no IPS (17/20, 144 ECTS) após 4,5 anos na Deloitte Portugal "
                    f"(início em Python, Java e PyTorch, seguido de SAP ABAP) e desenvolvi projetos testados em IA e Full-Stack no GitHub."
                )
            else:
                out[q] = (
                    f"I am applying {role_info['apply_phrase']} at {company} because I want to build reliable software and AI systems in production. "
                    f"I am completing my BSc in Computer Engineering at IPS (17/20 GPA, 144 ECTS) after 4.5 years at Deloitte Portugal "
                    f"(early Python, Java, and PyTorch before SAP ABAP delivery) and shipped tested AI/Full-Stack projects on GitHub."
                )
    return out
