"""Worker Playwright v2 (Human-in-the-Loop): sessão Chrome persistente, URL externo, cookies, dropdowns e perguntas IA."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from estagio_radar.ai_writer import answer_form_questions_with_gemini, load_cv_markdown
from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.autofill")

CHROME_PROFILE_DIR = Path("data/chrome_copilot_profile")

SUBMIT_BLOCK_RE = re.compile(r"\b(?:submit|send|confirm|finali[sz]e|submeter|enviar candidatura)\b", re.IGNORECASE)
APPLY_NAV_RE = re.compile(r"\b(?:apply(?:\s+(?:now|for this job|online))?|candidatar(?:-me)?|start application)\b", re.IGNORECASE)
COOKIE_TEXT_RE = re.compile(r"^(?:accept(?:\s+all(?:\s+cookies)?)?|aceitar(?:\s+tudo|\s+cookies)?|i agree|allow all)$", re.IGNORECASE)
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

    if tag == "textarea":
        if any(k in low for k in ("cover", "carta", "why", "motivation", "motivacao", "additional", "message", "summary")):
            return "cover_note"
        return "open_question"

    if input_type in {"hidden", "checkbox", "radio", "submit", "button"}:
        return None

    if ("first" in low and "name" in low) or "primeiro nome" in low or "fname" in low or "given-name" in low:
        return "first_name"
    if ("last" in low and "name" in low) or "apelido" in low or "surname" in low or "lname" in low or "family-name" in low:
        return "last_name"
    if "full name" in low or "nome completo" in low or re.search(r"(?:^|\b|\|)(?:name|nome)(?:$|\b|\|)", low):
        if not any(b in low for b in ("company", "empresa", "university", "school", "degree", "file")):
            return "full_name"
    if "email" in low or "e-mail" in low or input_type == "email":
        return "email"
    if "phone" in low or "mobile" in low or "telefone" in low or "telemovel" in low or "telemóvel" in low or input_type == "tel":
        return "phone"
    if "linkedin" in low:
        return "linkedin_url"
    if "github" in low:
        return "github_url"
    if any(k in low for k in ("portfolio", "website", "personal site", "blog", "url")):
        if "linkedin" not in low and "github" not in low:
            return "portfolio_url"
    if any(k in low for k in ("city", "cidade", "location", "localizacao", "localização", "morada", "address")):
        return "location"
    if any(k in low for k in ("university", "school", "college", "faculdade", "universidade", "instituicao", "instituição")):
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
        "country": str(profile.get("country") or "Portugal"),
        "university": str(profile.get("university") or ""),
        "degree": str(profile.get("degree") or ""),
        "graduation_year": str(profile.get("graduation_year") or "2027"),
        "cover_note": str(kit.get("short_cover_note") or ""),
    }


def _dismiss_cookie_banners(page: Any) -> None:
    try:
        for sel in ("#onetrust-accept-btn-handler", "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"):
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=1200)
                page.wait_for_timeout(300)
                return
        btns = page.locator("button, a[role='button']")
        for i in range(min(btns.count(), 30)):
            b = btns.nth(i)
            try:
                txt = (b.inner_text(timeout=250) or "").strip()
            except Exception:
                continue
            if txt and len(txt) <= 28 and COOKIE_TEXT_RE.match(txt):
                if b.is_visible():
                    b.click(timeout=1000)
                    page.wait_for_timeout(300)
                    break
    except Exception:
        pass


def _select_matching_option(frame: Any, meta: dict[str, Any], desired_tokens: list[str], fallback_label: str) -> str | None:
    try:
        select_loc = frame.locator("select").nth(meta["selectIdx"])
        options = meta.get("options") or []
        for tok in desired_tokens:
            for opt in options:
                txt = str(opt.get("text") or "").casefold()
                val = str(opt.get("value") or "")
                if val and tok.casefold() in txt:
                    select_loc.select_option(value=val, timeout=1200)
                    return fallback_label
    except Exception:
        return None
    return None


def _fill_selects_and_radios(frame: Any, profile: dict[str, Any]) -> list[str]:
    filled: list[str] = []
    try:
        selects = frame.eval_on_selector_all(
            "select",
            """els => els.map((el, idx) => {
                const id = el.id || "";
                const labelEl = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                const cont = el.closest("div, li, fieldset, section, td");
                const lbl = (labelEl?.innerText || el.closest("label")?.innerText || cont?.querySelector("label, legend")?.innerText || "").trim();
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0;
                return {
                    selectIdx: idx,
                    name: el.getAttribute("name") || "",
                    id,
                    label: lbl,
                    disabled: Boolean(el.disabled),
                    visible,
                    options: Array.from(el.options).map(o => ({ value: o.value, text: o.text }))
                };
            })""",
        )
    except Exception:
        selects = []

    for meta in selects:
        if meta.get("disabled") or not meta.get("visible"):
            continue
        desc = f"{meta.get('name', '')} | {meta.get('id', '')} | {meta.get('label', '')}".casefold()
        if any(k in desc for k in ("country", "pais", "país", "nation")):
            hit = _select_matching_option(frame, meta, ["portugal"], "country")
        elif any(k in desc for k in ("sponsor", "visa", "visto")):
            hit = _select_matching_option(frame, meta, ["no", "não", "nao", "do not require"], "sponsorship")
        elif any(k in desc for k in ("authori", "eligible", "work in", "trabalhar")):
            hit = _select_matching_option(frame, meta, ["yes", "sim", "authorized"], "work_auth")
        elif any(k in desc for k in ("graduation", "grad year", "conclus")):
            hit = _select_matching_option(frame, meta, [str(profile.get("graduation_year") or "2027")], "graduation_year")
        elif any(k in desc for k in ("degree", "education", "licenciatura", "habilit")):
            hit = _select_matching_option(frame, meta, ["bachelor", "bsc", "licenciatura"], "degree")
        elif any(k in desc for k in ("hear about", "source", "como conheceu")):
            hit = _select_matching_option(frame, meta, ["linkedin", "company", "website", "career"], "source")
        else:
            hit = None
        if hit:
            filled.append(hit)

    return filled


def _fill_frame_inputs(
    frame: Any,
    values: dict[str, str],
    cv_path: Path | None,
    profile: dict[str, Any],
) -> tuple[list[str], bool, list[tuple[int, str]]]:
    filled: list[str] = []
    cv_attached = False
    open_questions: list[tuple[int, str]] = []

    try:
        candidates = frame.eval_on_selector_all(
            "input, textarea",
            """els => els.map((el, idx) => {
                const id = el.id || "";
                const ariaLblBy = el.getAttribute("aria-labelledby") || "";
                const refEl = ariaLblBy ? document.getElementById(ariaLblBy.split(/\\s+/)[0]) : null;
                const labelEl = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                const parentLabel = el.closest("label");
                const container = el.closest("div, li, fieldset, section, td");
                const containerLabel = container ? container.querySelector("label, legend, span[class*='label' i]") : null;
                const labelTxt = (labelEl?.innerText || refEl?.innerText || parentLabel?.innerText || containerLabel?.innerText || "").trim();
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
        return filled, cv_attached, open_questions

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
        if kind == "open_question":
            q_label = (meta.get("label") or meta.get("ariaLabel") or meta.get("placeholder") or "").strip()
            if q_label and not str(meta.get("value") or "").strip():
                open_questions.append((meta["idx"], q_label[:220]))
            continue
        if kind and values.get(kind) and not str(meta.get("value") or "").strip():
            try:
                locator_all.nth(meta["idx"]).fill(values[kind], timeout=1500)
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

    filled.extend(_fill_selects_and_radios(frame, profile))
    return filled, cv_attached, open_questions


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
            if not txt or len(txt) > 45 or SUBMIT_BLOCK_RE.search(txt):
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


def _launch_persistent(p: Any, profile_dir: Path, *, headless: bool) -> Any:
    profile_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for channel in ("chrome", "msedge", None):
        try:
            kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": headless,
                "no_viewport": not headless,
                "args": ["--disable-blink-features=AutomationControlled"] + ([] if headless else ["--start-maximized"]),
            }
            if channel:
                kwargs["channel"] = channel
            return p.chromium.launch_persistent_context(**kwargs)
        except Exception as err:
            last_err = err
    raise RuntimeError(f"Não foi possível iniciar o browser persistente Chrome/Edge: {last_err}")


def open_login_browser(urls: list[str] | None = None, *, user_data_dir: Path | None = None) -> None:
    """Abre o perfil Chrome persistente para o utilizador iniciar sessão 1 vez no LinkedIn / portais."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError("Playwright não instalado neste ambiente.") from err

    targets = urls or [
        "https://www.linkedin.com/login",
        "https://www.itjobs.pt/",
        "https://pt.teamlyzer.com/users/login",
    ]
    prof_dir = user_data_dir or CHROME_PROFILE_DIR

    with sync_playwright() as p:
        context = _launch_persistent(p, prof_dir, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(targets[0], wait_until="domcontentloaded", timeout=20000)
            _inject_copilot_banner(
                page,
                "Sessão persistente Estágio Radar: inicia sessão no LinkedIn / portais (fica guardado para os auto-preenchimentos). Fecha o browser quando terminares.",
            )
            for extra in targets[1:]:
                tab = context.new_page()
                tab.goto(extra, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
        finally:
            try:
                context.close()
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
    user_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Abre o Chrome persistente na vaga, fecha cookies, pré-preenche campos/selects/perguntas e anexa o CV."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError(
            "Playwright não instalado neste ambiente. Instala com: uv pip install playwright"
        ) from err

    url = target_url or kit.get("external_apply_url") or job.job_url
    cv_raw = str((kit.get("cv") or {}).get("path") or "").strip()
    cv_path = Path(cv_raw) if cv_raw else None
    values = _build_field_values(profile, kit)

    filled_all: list[str] = []
    cv_attached = False
    final_url = url

    temp_ctx = tempfile.TemporaryDirectory() if (headless and user_data_dir is None) else None
    prof_dir = user_data_dir or (Path(temp_ctx.name) if temp_ctx is not None else CHROME_PROFILE_DIR)

    try:
        with sync_playwright() as p:
            context = _launch_persistent(p, prof_dir, headless=headless)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=22000)
                page.wait_for_timeout(900)
                _dismiss_cookie_banners(page)
                _maybe_open_apply_view(page)

                frames_to_scan = [page.main_frame]
                for fr in page.frames:
                    if fr == page.main_frame:
                        continue
                    if ATS_FRAME_HINT_RE.search(fr.url or "") or fr.url.startswith("file://"):
                        frames_to_scan.append(fr)

                pending_questions: list[tuple[Any, int, str]] = []
                for fr in frames_to_scan:
                    f_names, got_cv, open_qs = _fill_frame_inputs(
                        fr, values, cv_path if not cv_attached else None, profile
                    )
                    filled_all.extend(f_names)
                    if got_cv:
                        cv_attached = True
                    for idx, qtxt in open_qs:
                        pending_questions.append((fr, idx, qtxt))

                if pending_questions:
                    q_texts = [q for _, _, q in pending_questions]
                    try:
                        page_txt = (page.inner_text("body") or "")[:3000]
                    except Exception:
                        page_txt = ""
                    answers = answer_form_questions_with_gemini(
                        questions=q_texts,
                        company=job.company,
                        title=job.title,
                        page_text=page_txt,
                        cv_markdown=load_cv_markdown(cv_path) if cv_path else "",
                        profile=profile,
                        language=str(kit.get("language") or "en"),
                    )
                    for fr, idx, qtxt in pending_questions:
                        ans = answers.get(qtxt)
                        if ans:
                            try:
                                fr.locator("input, textarea").nth(idx).fill(ans, timeout=1500)
                                filled_all.append("open_question")
                            except Exception:
                                pass

                cv_name = cv_path.name if cv_path else "N/D"
                unique_fields = list(dict.fromkeys(filled_all))
                banner_msg = (
                    f"Estágio Radar Copilot: {len(unique_fields)} campo(s) ({', '.join(unique_fields[:6])})"
                    + (f" + CV anexado ({cv_name})" if cv_attached else f" (anexa manualmente: {cv_name})")
                    + " — Revê e clica em Submeter!"
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
                    context.close()
                except Exception:
                    pass
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()

    return {
        "ok": True,
        "filled_fields": list(dict.fromkeys(filled_all)),
        "cv_attached": cv_attached,
        "cv_filename": cv_path.name if cv_path else "",
        "url": final_url,
    }
