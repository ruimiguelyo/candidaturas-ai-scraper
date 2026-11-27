"""Worker Playwright (Human-in-the-Loop) para abrir e auto-preencher formulários ATS no Chrome visível."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.autofill")

SUBMIT_BLOCK_RE = re.compile(r"\b(?:submit|send|confirm|finali[sz]e|submeter|enviar candidatura)\b", re.IGNORECASE)
APPLY_NAV_RE = re.compile(r"\b(?:apply(?:\s+(?:now|for this job|online))?|candidatar(?:-me)?|start application)\b", re.IGNORECASE)
ATS_FRAME_HINT_RE = re.compile(
    r"(?:greenhouse|ashby|lever|smartrecruiters|recruitee|teamtailor|workday|bamboohr|breezy|successfactors|avature)",
    re.IGNORECASE,
)


def _match_field_kind(descriptor: str, tag: str, input_type: str) -> str | None:
    low = descriptor.casefold()
    if input_type == "file":
        if any(k in low for k in ("photo", "avatar", "picture", "image", "cover_letter", "cover letter", "carta")):
            return None
        if any(k in low for k in ("resume", "cv", "curriculum", "file", "upload", "anexo", "attach")):
            return "resume"
        return "resume_fallback"

    if tag == "textarea" or any(k in low for k in ("cover", "carta", "motivation", "motivacao", "why", "additional", "message", "note", "summary")):
        if tag == "textarea":
            return "cover_note"
        return None

    if input_type in {"hidden", "checkbox", "radio", "submit", "button"}:
        return None

    if "first" in low and "name" in low or "primeiro nome" in low or "fname" in low:
        return "first_name"
    if "last" in low and "name" in low or "apelido" in low or "surname" in low or "lname" in low:
        return "last_name"
    if "full name" in low or "nome completo" in low or re.search(r"(?:^|\b)(?:name|nome)(?:$|\b)", low):
        if not any(b in low for b in ("company", "empresa", "university", "school", "degree", "file")):
            return "full_name"
    if "email" in low or "e-mail" in low or input_type == "email":
        return "email"
    if "phone" in low or "mobile" in low or "telefone" in low or "telemovel" in low or input_type == "tel":
        return "phone"
    if "linkedin" in low:
        return "linkedin_url"
    if "github" in low:
        return "github_url"
    if any(k in low for k in ("portfolio", "website", "personal site", "blog", "url")):
        if "linkedin" not in low and "github" not in low:
            return "portfolio_url"
    if any(k in low for k in ("city", "cidade", "location", "localizacao", "morada", "address")):
        return "location"
    if any(k in low for k in ("university", "school", "college", "faculdade", "universidade", "instituicao")):
        return "university"
    if any(k in low for k in ("degree", "curso", "licenciatura", "major", "field of study")):
        return "degree"
    if any(k in low for k in ("graduation", "grad year", "ano de conclusao")):
        return "graduation_year"
    return None


def _build_field_values(profile: dict[str, Any], kit: dict[str, Any]) -> dict[str, str]:
    return {
        "first_name": str(profile.get("first_name") or ""),
        "last_name": str(profile.get("last_name") or ""),
        "full_name": str(profile.get("full_name") or ""),
        "email": str(profile.get("email") or ""),
        "phone": str(profile.get("phone") or ""),
        "linkedin_url": str(profile.get("linkedin_url") or ""),
        "github_url": str(profile.get("github_url") or ""),
        "portfolio_url": str(profile.get("portfolio_url") or ""),
        "location": str(profile.get("location") or profile.get("city") or ""),
        "university": str(profile.get("university") or ""),
        "degree": str(profile.get("degree") or ""),
        "graduation_year": str(profile.get("graduation_year") or ""),
        "cover_note": str(kit.get("short_cover_note") or ""),
    }


def _fill_frame_inputs(frame: Any, values: dict[str, str], cv_path: Path | None) -> tuple[list[str], bool]:
    filled: list[str] = []
    cv_attached = False
    try:
        candidates = frame.eval_on_selector_all(
            "input, textarea",
            """els => els.map((el, idx) => {
                const id = el.id || "";
                const labelEl = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                const parentLabel = el.closest("label");
                const container = el.closest("div, li, fieldset, section, td");
                const containerLabel = container ? container.querySelector("label, legend") : null;
                const labelTxt = (labelEl?.innerText || parentLabel?.innerText || containerLabel?.innerText || "").trim();
                const rect = el.getBoundingClientRect();
                const visible = el.type === "file" ? true : (rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden");
                return {
                    idx,
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute("type") || "text").toLowerCase(),
                    name: el.getAttribute("name") || "",
                    id,
                    placeholder: el.getAttribute("placeholder") || "",
                    ariaLabel: el.getAttribute("aria-label") || "",
                    autocomplete: el.getAttribute("autocomplete") || "",
                    label: labelTxt,
                    value: el.value || "",
                    disabled: Boolean(el.disabled || el.readOnly),
                    visible
                };
            })""",
        )
    except Exception:
        return filled, cv_attached

    locator_all = frame.locator("input, textarea")
    fallback_file_indices: list[int] = []

    for meta in candidates:
        if meta.get("disabled") or not meta.get("visible"):
            continue
        descriptor = " | ".join(
            str(meta.get(k) or "") for k in ("name", "id", "placeholder", "ariaLabel", "autocomplete", "label")
        )
        kind = _match_field_kind(descriptor, meta["tag"], meta["type"])
        if kind == "resume_fallback":
            fallback_file_indices.append(meta["idx"])
            continue
        if kind == "resume":
            if cv_path and cv_path.exists() and not cv_attached:
                try:
                    locator_all.nth(meta["idx"]).set_input_files(str(cv_path))
                    cv_attached = True
                    filled.append("cv_pdf")
                except Exception:
                    pass
            continue
        if kind and values.get(kind) and not str(meta.get("value") or "").strip():
            try:
                loc = locator_all.nth(meta["idx"])
                loc.fill(values[kind], timeout=1500)
                filled.append(kind)
            except Exception:
                pass

    if not cv_attached and fallback_file_indices and cv_path and cv_path.exists():
        try:
            locator_all.nth(fallback_file_indices[0]).set_input_files(str(cv_path))
            cv_attached = True
            filled.append("cv_pdf")
        except Exception:
            pass

    return filled, cv_attached


def _maybe_open_apply_view(page: Any) -> None:
    """Se ainda não houver inputs visíveis, clica no botão 'Apply / Candidatar' (NUNCA em Submit)."""
    try:
        has_inputs = page.locator("input[type='email'], input[type='file'], form input[name*='name' i]").count() > 0
        if has_inputs:
            return
    except Exception:
        return

    try:
        if "jobs.lever.co" in page.url.casefold() and not page.url.rstrip("/").endswith("/apply"):
            page.goto(page.url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=12000)
            return
    except Exception:
        pass

    try:
        candidates = page.locator("a, button")
        count = min(candidates.count(), 45)
        for i in range(count):
            el = candidates.nth(i)
            try:
                txt = (el.inner_text(timeout=400) or "").strip()
            except Exception:
                continue
            if not txt or len(txt) > 45:
                continue
            if SUBMIT_BLOCK_RE.search(txt):
                continue
            if APPLY_NAV_RE.search(txt):
                try:
                    if el.is_visible():
                        el.click(timeout=2000)
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    continue
    except Exception:
        pass


def _inject_copilot_banner(page: Any, summary: str) -> None:
    try:
        page.evaluate(
            """(msg) => {
                const existing = document.getElementById('estagio-radar-copilot-banner');
                if (existing) existing.remove();
                const bar = document.createElement('div');
                bar.id = 'estagio-radar-copilot-banner';
                bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#0f172a;color:#f8fafc;border-bottom:2px solid #38bdf8;padding:10px 16px;font:600 13px/1.4 system-ui,sans-serif;display:flex;justify-content:space-between;align-items:center;box-shadow:0 4px 16px rgba(0,0,0,0.35)';
                const span = document.createElement('span');
                span.textContent = msg;
                const btn = document.createElement('button');
                btn.textContent = 'Fechar aviso';
                btn.style.cssText = 'background:#334155;color:#fff;border:none;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px;margin-left:12px';
                btn.onclick = () => bar.remove();
                bar.appendChild(span);
                bar.appendChild(btn);
                document.body && document.body.prepend(bar);
            }""",
            summary,
        )
    except Exception:
        pass


def run_autofill_browser(
    job: EnrichedJob,
    kit: dict[str, Any],
    profile: dict[str, Any],
    *,
    headless: bool = False,
    keep_open: bool = True,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Abre o Chrome visível na vaga, pré-preenche dados e anexa o CV sem nunca submeter sozinho."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError(
            "Playwright não instalado neste ambiente. Instala com: uv pip install playwright"
        ) from err

    url = target_url or job.job_url
    cv_raw = str((kit.get("cv") or {}).get("path") or "").strip()
    cv_path = Path(cv_raw) if cv_raw else None
    values = _build_field_values(profile, kit)

    filled_all: list[str] = []
    cv_attached = False
    final_url = url

    with sync_playwright() as p:
        browser = None
        last_err: Exception | None = None
        for channel in ("chrome", "msedge", None):
            try:
                kwargs: dict[str, Any] = {"headless": headless}
                if not headless:
                    kwargs["args"] = ["--start-maximized"]
                if channel:
                    kwargs["channel"] = channel
                browser = p.chromium.launch(**kwargs)
                break
            except Exception as err:
                last_err = err
        if browser is None:
            raise RuntimeError(f"Não foi possível iniciar o browser Chrome/Edge: {last_err}")

        context = browser.new_context(no_viewport=not headless)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(900)
            _maybe_open_apply_view(page)

            frames_to_scan = [page.main_frame]
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                if ATS_FRAME_HINT_RE.search(fr.url or "") or fr.url.startswith("file://"):
                    frames_to_scan.append(fr)

            for fr in frames_to_scan:
                f_names, got_cv = _fill_frame_inputs(fr, values, cv_path if not cv_attached else None)
                filled_all.extend(f_names)
                if got_cv:
                    cv_attached = True

            cv_name = cv_path.name if cv_path else "N/D"
            unique_fields = list(dict.fromkeys(filled_all))
            banner_msg = (
                f"Estágio Radar Copilot: {len(unique_fields)} campo(s) pré-preenchido(s)"
                + (f" + CV anexado ({cv_name})" if cv_attached else f" (anexa manualmente: {cv_name})")
                + " — Revê, passa CAPTCHA se existir e clica em Submeter!"
            )
            _inject_copilot_banner(page, banner_msg)
            final_url = page.url

            if keep_open and not headless:
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:
                    pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    return {
        "ok": True,
        "filled_fields": list(dict.fromkeys(filled_all)),
        "cv_attached": cv_attached,
        "cv_filename": cv_path.name if cv_path else "",
        "url": final_url,
    }
