"""Gerador de CV PDF adaptado em 1 página A4 por vaga (headline, resumo e ordem de projetos reais)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from estagio_radar.ai_writer import (
    _candidate_models,
    _is_transient_model_error,
    _make_gemini_client,
    load_cv_markdown,
)
from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.cv_tailor")

KEYWORD_BANK = (
    "python",
    "pytorch",
    "jax",
    "flax",
    "llm",
    "genai",
    "machine learning",
    "deep learning",
    "nlp",
    "computer vision",
    "sql",
    "postgresql",
    "typescript",
    "react",
    "node.js",
    "docker",
    "kubernetes",
    "fastapi",
    "ci/cd",
    "pytest",
    "sap",
    "abap",
    "data",
    "analytics",
    "evaluation",
    "transformers",
)


class TailoredCvPatch(BaseModel):
    """Ajustes permitidos ao CV HTML base sem nunca alterar factos nem exceder 1 página A4."""

    headline: str = Field(description="Headline concisa de 1 linha alinhada à vaga (máximo 115 caracteres).")
    summary_plain: str = Field(
        description="Resumo profissional em 2 frases diretas e verdadeiras com foco nos requisitos da vaga (máximo 430 caracteres)."
    )
    project_order: list[int] = Field(
        default_factory=list,
        description="Permutação dos índices dos projetos existentes por ordem de relevância para esta vaga.",
    )
    matched_keywords: list[str] = Field(
        default_factory=list,
        description="Até 6 palavras-chave técnicas reais partilhadas entre a vaga e o CV.",
    )


def _slug_token(text: str, *, default: str = "Job") -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return (clean[:24] or default)


def _extract_project_titles(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for sec in soup.find_all("section", class_="section"):
        entries = sec.find_all("div", class_="entry", recursive=False)
        if len(entries) >= 2:
            titles: list[str] = []
            for ent in entries:
                t_el = ent.find(class_="entry-title")
                titles.append((t_el.get_text(" ", strip=True) if t_el else "Project")[:100])
            return titles
    return []


def _offline_tailored_patch(
    *,
    job: EnrichedJob,
    description: str,
    project_titles: list[str],
    html_text: str,
) -> TailoredCvPatch:
    blob = f"{job.title} {description} {' '.join(job.tags)}".casefold()
    matched = [kw for kw in KEYWORD_BANK if kw in blob][:6]

    soup = BeautifulSoup(html_text, "html.parser")
    orig_headline_el = soup.select_one(".headline")
    orig_summary_el = soup.select_one("p.summary")
    orig_headline = orig_headline_el.get_text(" ", strip=True) if orig_headline_el else job.title
    orig_summary = orig_summary_el.get_text(" ", strip=True) if orig_summary_el else ""

    if project_titles:
        # Ordena os projetos reais pela sobreposição de keywords com o anúncio
        ranked = sorted(
            enumerate(project_titles),
            key=lambda pair: sum(1 for tok in re.findall(r"[a-z0-9+.#-]{3,}", pair[1].casefold()) if tok in blob),
            reverse=True,
        )
        order = [idx for idx, _ in ranked]
    else:
        order = []

    if matched:
        top_tags = ", ".join(k.upper() if k in {"ai", "ml", "llm", "nlp", "sql", "jax", "sap"} else k.title() for k in matched[:4])
        headline = f"{job.title[:65]} Candidate | {top_tags}"[:115]
    else:
        headline = orig_headline[:115]

    return TailoredCvPatch(
        headline=headline,
        summary_plain=(orig_summary or f"BSc Computer Engineering student (17/20 GPA, 144 ECTS) targeting {job.title} at {job.company}.")[:430],
        project_order=order,
        matched_keywords=matched,
    )


def generate_tailored_patch(
    *,
    job: EnrichedJob,
    description: str,
    cv_markdown: str,
    project_titles: list[str],
    html_text: str,
    client: Any = None,
) -> tuple[TailoredCvPatch, str]:
    """Gera patch factual de CV para a vaga via Gemini (com fallback determinístico rápido)."""
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if client is not None or api_key:
        try:
            active_client = client or _make_gemini_client(api_key, timeout_ms=12000)
            n_proj = len(project_titles)
            proj_lines = "\n".join(f"{i}: {t}" for i, t in enumerate(project_titles))
            sys_inst = (
                "You tailor a 1-page software/AI engineering CV HTML for a specific job WITHOUT ever inventing facts. "
                "CRITICAL RULES:\n"
                "1. NEVER invent skills, companies, degrees, metrics, or experiences not present in the provided CV.\n"
                "2. Keep 'headline' under 115 characters and 'summary_plain' under 430 characters (2 crisp sentences) so the CV remains strictly 1 A4 page.\n"
                f"3. 'project_order' MUST be a permutation of {list(range(n_proj))} ranking the existing projects by relevance to the job."
            )
            prompt = (
                f"TARGET COMPANY: {job.company} | ROLE: {job.title}\n"
                f"JOB DESCRIPTION:\n{description[:3800]}\n\n"
                f"EXISTING PROJECT INDICES:\n{proj_lines}\n\n"
                f"REAL CV CONTENT:\n{cv_markdown[:4500]}"
            )
            for model_name in _candidate_models():
                try:
                    interaction = active_client.interactions.create(
                        model=model_name,
                        system_instruction=sys_inst,
                        input=prompt,
                        response_format={
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": TailoredCvPatch.model_json_schema(),
                        },
                        store=False,
                        timeout=10.0,
                    )
                    raw_out = getattr(interaction, "output_text", None) or ""
                    patch = TailoredCvPatch.model_validate_json(raw_out)
                    return patch, model_name
                except Exception as err:
                    if _is_transient_model_error(err):
                        logger.warning("Fallback modelo CV tailor (%s): %s", model_name, err)
                        continue
                    logger.warning("Erro Gemini CV tailor (%s): %s", model_name, err)
                    break
        except Exception as err:
            logger.warning("Fallback offline CV tailor: %s", err)

    offline = _offline_tailored_patch(
        job=job,
        description=description,
        project_titles=project_titles,
        html_text=html_text,
    )
    return offline, "offline-keywords"


def tailor_cv_html(base_html: str, patch: TailoredCvPatch) -> str:
    """Aplica headline, resumo curto e ordem dos projetos ao HTML base garantindo 1 página."""
    soup = BeautifulSoup(base_html, "html.parser")

    head_el = soup.select_one(".headline")
    clean_headline = re.sub(r"\s+", " ", patch.headline or "").strip()[:125]
    if head_el is not None and clean_headline:
        head_el.clear()
        head_el.append(clean_headline)

    sum_el = soup.select_one("p.summary")
    clean_summary = re.sub(r"\s+", " ", patch.summary_plain or "").strip()[:460]
    if sum_el is not None and clean_summary:
        sum_el.clear()
        sum_el.append(clean_summary)

    for sec in soup.find_all("section", class_="section"):
        entries = sec.find_all("div", class_="entry", recursive=False)
        if len(entries) >= 2:
            order = patch.project_order or []
            if len(order) == len(entries) and set(order) == set(range(len(entries))):
                detached = [ent.extract() for ent in entries]
                for idx in order:
                    sec.append(detached[idx])
            break

    return str(soup)


def render_html_to_one_page_pdf(html_path: Path, pdf_path: Path) -> Path:
    """Renderiza HTML em PDF A4 estritamente com 1 página via Playwright headless."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError("Playwright necessário para renderizar o CV PDF.") from err

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    with sync_playwright() as p:
        for channel in ("chrome", "msedge", None):
            browser = None
            try:
                kwargs: dict[str, Any] = {"headless": True}
                if channel:
                    kwargs["channel"] = channel
                browser = p.chromium.launch(**kwargs)
                page = browser.new_page()
                page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                    page_ranges="1",
                )
                return pdf_path
            except Exception as err:
                last_err = err
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    raise RuntimeError(f"Falha ao gerar PDF A4 via Playwright: {last_err}")


def generate_tailored_cv_pdf(
    job: EnrichedJob,
    *,
    base_cv_pdf: Path,
    description: str,
    profile: dict[str, Any],
    out_dir: Path,
    client: Any = None,
) -> dict[str, Any]:
    """Gera HTML e PDF de 1 página adaptado à vaga mantendo dados 100% verídicos."""
    html_src = base_cv_pdf.with_suffix(".html")
    if not html_src.exists():
        raise FileNotFoundError(f"Template HTML do CV base não encontrado em '{html_src}'.")

    base_html = html_src.read_text(encoding="utf-8")
    cv_md = load_cv_markdown(base_cv_pdf)
    project_titles = _extract_project_titles(base_html)

    patch, model_used = generate_tailored_patch(
        job=job,
        description=description,
        cv_markdown=cv_md,
        project_titles=project_titles,
        html_text=base_html,
        client=client,
    )
    tailored_html = tailor_cv_html(base_html, patch)

    first_tok = _slug_token(str(profile.get("first_name") or "Candidate"), default="Candidate")
    last_tok = _slug_token(str(profile.get("last_name") or ""), default="")
    name_prefix = f"{first_tok}_{last_tok}" if last_tok else first_tok
    comp_slug = _slug_token(job.company, default="Company")
    stem = f"{name_prefix}_CV_{comp_slug}_{job.fingerprint[:6]}"

    out_dir.mkdir(parents=True, exist_ok=True)
    tailored_html_path = out_dir / f"{stem}.html"
    tailored_pdf_path = out_dir / f"{stem}.pdf"

    tailored_html_path.write_text(tailored_html, encoding="utf-8")
    render_html_to_one_page_pdf(tailored_html_path, tailored_pdf_path)

    return {
        "ok": True,
        "path": str(tailored_pdf_path.resolve()),
        "filename": tailored_pdf_path.name,
        "html_path": str(tailored_html_path.resolve()),
        "headline": patch.headline,
        "summary": patch.summary_plain,
        "matched_keywords": patch.matched_keywords,
        "project_order": patch.project_order,
        "model": model_used,
    }
