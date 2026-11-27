"""Worker Playwright v3 (Plataforma Única): Chrome dedicado via CDP, Easy Apply, Comboboxes, Radios e guarda CAPTCHA."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from estagio_radar.ai_writer import answer_form_questions_with_gemini, load_cv_markdown
from estagio_radar.models import EnrichedJob

logger = logging.getLogger("estagio_radar.autofill")

CHROME_PROFILE_DIR = Path("data/chrome_copilot_profile")
AUTOFILL_STATUS_PATH = Path("data/last_autofill.json")
CDP_PORT = 9229

SUBMIT_BLOCK_RE = re.compile(
    r"\b(?:submit(?:\s+application)?|send(?:\s+application)?|confirm|finali[sz]e|submeter(?:\s+candidatura)?|enviar\s+candidatura)\b",
    re.IGNORECASE,
)
APPLY_NAV_RE = re.compile(r"\b(?:apply(?:\s+(?:now|for this job|online))?|candidatar(?:-me)?|start application)\b", re.IGNORECASE)
EASY_APPLY_BTN_RE = re.compile(r"\b(?:easy\s+apply|candidatura\s+simplificada)\b", re.IGNORECASE)
NEXT_STEP_RE = re.compile(r"^(?:next|seguinte|continue(?:\s+to\s+next\s+step)?|continuar|review|rever)$", re.IGNORECASE)
COOKIE_TEXT_RE = re.compile(
    r"^(?:accept(?:\s+all(?:\s+cookies)?)?|aceitar(?:\s+tudo|\s+cookies)?|i agree|allow all)$",
    re.IGNORECASE,
)
CAPTCHA_SELECTOR = (
    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
    "iframe[src*='challenges.cloudflare.com'], iframe[title*='recaptcha' i], "
    "iframe[title*='hcaptcha' i], .g-recaptcha, .h-captcha, .cf-turnstile"
)
CAPTCHA_TEXT_RE = re.compile(
    r"\b(?:verify you are human|confirm you are human|não sou um robô|i['’]?m not a robot)\b",
    re.IGNORECASE,
)
ATS_FRAME_HINT_RE = re.compile(
    r"(?:greenhouse|ashby|lever|smartrecruiters|recruitee|teamtailor|workday|bamboohr|breezy|successfactors|avature)",
    re.IGNORECASE,
)


def write_autofill_status(payload: dict[str, Any], path: Path = AUTOFILL_STATUS_PATH) -> None:
    """Persiste estado estruturado da última execução para feedback live no Dashboard."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        enriched = {
            **payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as err:
        logger.debug("Não foi possível escrever estado autofill em %s: %s", path, err)


def read_autofill_status(path: Path = AUTOFILL_STATUS_PATH) -> dict[str, Any]:
    """Lê o estado mais recente do autofill para polling no Dashboard."""
    if not path.exists():
        return {"status": "idle", "message": "Sem execuções recentes."}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"status": "idle"}
    except Exception:
        return {"status": "idle"}


def _find_chrome_executable() -> Path | None:
    """Encontra o binário local do Google Chrome ou Microsoft Edge no Windows/Linux."""
    candidates: list[Path] = []
    if os.name == "nt":
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key, "").strip()
            if not base:
                continue
            candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
            candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    for cmd_name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(cmd_name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.exists():
            return path
    return None


def _cdp_endpoint_ready(port: int = CDP_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.6) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ensure_cdp_chrome(profile_dir: Path, port: int = CDP_PORT) -> str | None:
    """Reutiliza ou arranca o Chrome dedicado com porta CDP fixa para nunca colidir perfis."""
    endpoint = f"http://127.0.0.1:{port}"
    if _cdp_endpoint_ready(port):
        return endpoint

    exe = _find_chrome_executable()
    if exe is None:
        return None

    abs_profile = profile_dir.resolve()
    abs_profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(exe),
        f"--user-data-dir={abs_profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "about:blank",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=(os.name != "nt"),
        )
    except Exception as err:
        logger.warning("Falha ao arrancar Chrome CDP em %s: %s", exe, err)
        return None

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _cdp_endpoint_ready(port):
            return endpoint
        time.sleep(0.25)
    return None


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


def _detect_captcha(page: Any) -> bool:
    """Deteta CAPTCHAs / anti-bot visíveis sem nunca tentar resolvê-los nem contorná-los."""
    frames = [page.main_frame, *[fr for fr in page.frames if fr != page.main_frame]]
    for fr in frames:
        try:
            loc = fr.locator(CAPTCHA_SELECTOR)
            for idx in range(min(loc.count(), 6)):
                try:
                    if loc.nth(idx).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        try:
            has_challenge_txt = fr.evaluate(
                """() => {
                    const els = document.querySelectorAll("h1, h2, p, label, div[class*='challenge' i], div[class*='captcha' i]");
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const txt = (el.innerText || "").trim();
                        if (txt && txt.length <= 220 && /(verify you are human|confirm you are human|não sou um robô|i['’]?m not a robot)/i.test(txt)) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if has_challenge_txt:
                return True
        except Exception:
            pass
    return False


def _match_rule_for_descriptor(desc: str, profile: dict[str, Any]) -> tuple[list[str], str] | None:
    low = desc.casefold()
    if any(k in low for k in ("country", "pais", "país", "nation")):
        return ([str(profile.get("country") or "Portugal"), "portugal"], "country")
    if any(k in low for k in ("sponsor", "visa", "visto")):
        return (["no", "não", "nao", "do not require"], "sponsorship")
    if any(k in low for k in ("authori", "eligible", "right to work", "work in", "trabalhar", "eu citizen")):
        return (["yes", "sim", "authorized"], "work_auth")
    if any(k in low for k in ("graduation", "grad year", "conclus")):
        return ([str(profile.get("graduation_year") or "2027")], "graduation_year")
    if any(k in low for k in ("degree", "education", "licenciatura", "habilit")):
        return (["bachelor", "bsc", "licenciatura"], "degree")
    if any(k in low for k in ("hear about", "source", "como conheceu")):
        return (["linkedin", "company", "website", "career"], "source")
    return None


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


def _fill_selects(frame: Any, profile: dict[str, Any]) -> list[str]:
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
        desc = f"{meta.get('name', '')} | {meta.get('id', '')} | {meta.get('label', '')}"
        rule = _match_rule_for_descriptor(desc, profile)
        if rule is None:
            continue
        tokens, label_name = rule
        hit = _select_matching_option(frame, meta, tokens, label_name)
        if hit:
            filled.append(hit)
    return filled


def _fill_comboboxes(frame: Any, profile: dict[str, Any]) -> list[str]:
    """Preenche React/ARIA comboboxes (Greenhouse/Ashby/SmartRecruiters/Workday)."""
    filled: list[str] = []
    try:
        combos = frame.eval_on_selector_all(
            "[role='combobox']",
            """els => els.map((el, idx) => {
                const id = el.id || "";
                const ariaLblBy = el.getAttribute("aria-labelledby") || "";
                const refEl = ariaLblBy ? document.getElementById(ariaLblBy.split(/\\s+/)[0]) : null;
                const labelEl = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                const cont = el.closest("div, li, fieldset, section, td");
                const contLbl = cont ? cont.querySelector("label, legend") : null;
                const lbl = (labelEl?.innerText || refEl?.innerText || el.getAttribute("aria-label") || contLbl?.innerText || "").trim();
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden";
                const curVal = el.tagName === "INPUT" ? (el.value || "") : (el.getAttribute("aria-valuetext") || el.innerText || "");
                return {
                    comboIdx: idx,
                    tag: el.tagName.toLowerCase(),
                    name: el.getAttribute("name") || "",
                    id,
                    label: lbl,
                    value: curVal.trim(),
                    disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
                    visible
                };
            })""",
        )
    except Exception:
        return filled

    locator_all = frame.locator("[role='combobox']")
    for meta in combos:
        if meta.get("disabled") or not meta.get("visible"):
            continue
        cur_val = str(meta.get("value") or "").casefold()
        if cur_val and cur_val not in {"select", "select...", "choose", "selecionar", "--"}:
            continue
        desc = f"{meta.get('name', '')} | {meta.get('id', '')} | {meta.get('label', '')}"
        rule = _match_rule_for_descriptor(desc, profile)
        if rule is None:
            continue
        tokens, label_name = rule
        el = locator_all.nth(meta["comboIdx"])
        try:
            el.click(timeout=1200)
            if meta["tag"] == "input":
                el.fill(tokens[0], timeout=1200)
            page_or_frame = frame
            if hasattr(page_or_frame, "wait_for_timeout"):
                page_or_frame.wait_for_timeout(200)
            options = frame.locator("[role='option'], [class*='option' i]")
            matched = False
            opt_cnt = min(options.count(), 25)
            for tok in tokens:
                for i in range(opt_cnt):
                    opt = options.nth(i)
                    try:
                        if not opt.is_visible():
                            continue
                        opt_txt = (opt.inner_text(timeout=250) or "").strip().casefold()
                    except Exception:
                        continue
                    if tok.casefold() in opt_txt:
                        opt.click(timeout=1200)
                        matched = True
                        filled.append(label_name)
                        break
                if matched:
                    break
            if not matched and meta["tag"] == "input":
                el.press("Enter")
                filled.append(label_name)
        except Exception:
            continue
    return filled


def _fill_radio_groups(frame: Any, profile: dict[str, Any]) -> list[str]:
    """Preenche grupos de botões radio para elegibilidade/visto/localização."""
    filled: list[str] = []
    try:
        groups = frame.eval_on_selector_all(
            "fieldset, [role='radiogroup']",
            """els => els.map((grp, gIdx) => {
                const radios = Array.from(grp.querySelectorAll("input[type='radio']"));
                if (!radios.length) return null;
                const alreadyChecked = radios.some(r => r.checked);
                const head = (grp.querySelector("legend, label, p, span[class*='label' i]")?.innerText || grp.getAttribute("aria-label") || "").trim();
                const items = radios.map((r, rIdx) => {
                    const rid = r.id || "";
                    const lbl = rid ? document.querySelector(`label[for="${CSS.escape(rid)}"]`) : null;
                    const txt = (lbl?.innerText || r.closest("label")?.innerText || r.parentElement?.innerText || r.value || "").trim();
                    const rect = (lbl || r.closest("label") || r).getBoundingClientRect();
                    return {
                        rIdx,
                        text: txt,
                        value: r.value || "",
                        disabled: Boolean(r.disabled),
                        visible: rect.width > 0 && rect.height > 0
                    };
                });
                return { gIdx, question: head, alreadyChecked, items };
            }).filter(Boolean)""",
        )
    except Exception:
        return filled

    group_locs = frame.locator("fieldset, [role='radiogroup']")
    for grp in groups:
        if grp.get("alreadyChecked"):
            continue
        q_low = str(grp.get("question") or "").casefold()
        if any(k in q_low for k in ("sponsor", "visa", "visto")):
            target_re = re.compile(r"^(?:no|não|nao)\b", re.IGNORECASE)
            field_tag = "sponsorship"
        elif any(k in q_low for k in ("authori", "eligible", "right to work", "legal", "trabalhar", "eu citizen", "portugal", "hybrid", "commute")):
            target_re = re.compile(r"^(?:yes|sim)\b", re.IGNORECASE)
            field_tag = "work_auth"
        else:
            continue

        grp_loc = group_locs.nth(grp["gIdx"])
        for item in grp.get("items") or []:
            if item.get("disabled") or not item.get("visible"):
                continue
            cand_txt = f"{item.get('text', '')} {item.get('value', '')}".strip()
            if target_re.search(cand_txt):
                try:
                    grp_loc.locator("input[type='radio']").nth(item["rIdx"]).check(force=True, timeout=1200)
                    filled.append(field_tag)
                    break
                except Exception:
                    continue
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
                let ancestorVisible = true;
                let anc = el.parentElement;
                while (anc && anc !== document.body && anc !== document.documentElement) {
                    const st = getComputedStyle(anc);
                    if (st.display === "none" || st.visibility === "hidden") {
                        ancestorVisible = false;
                        break;
                    }
                    anc = anc.parentElement;
                }
                const visible = el.type === "file" ? ancestorVisible : (rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden" && ancestorVisible);
                return {
                    idx,
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute("type") || "text").toLowerCase(),
                    role: (el.getAttribute("role") || "").toLowerCase(),
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
        if meta.get("role") == "combobox":
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

    filled.extend(_fill_selects(frame, profile))
    filled.extend(_fill_comboboxes(frame, profile))
    filled.extend(_fill_radio_groups(frame, profile))
    return filled, cv_attached, open_questions


def _fill_pending_questions(
    page: Any,
    pending_questions: list[tuple[Any, int, str]],
    *,
    job: EnrichedJob,
    kit: dict[str, Any],
    profile: dict[str, Any],
    cv_path: Path | None,
) -> list[str]:
    if not pending_questions:
        return []
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
    filled: list[str] = []
    for fr, idx, qtxt in pending_questions:
        ans = answers.get(qtxt)
        if ans:
            try:
                fr.locator("input, textarea").nth(idx).fill(ans, timeout=1500)
                filled.append("open_question")
            except Exception:
                pass
    return filled


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


def _has_easy_apply_trigger(page: Any) -> Any | None:
    try:
        btns = page.locator("button.jobs-apply-button, button[data-easy-apply], button")
        for i in range(min(btns.count(), 45)):
            b = btns.nth(i)
            try:
                if not b.is_visible():
                    continue
                txt = (b.inner_text(timeout=300) or "").strip()
            except Exception:
                continue
            if txt and len(txt) <= 50 and not SUBMIT_BLOCK_RE.search(txt) and EASY_APPLY_BTN_RE.search(txt):
                return b
    except Exception:
        pass
    return None


def _is_linkedin_login_wall(page: Any) -> bool:
    low_url = (page.url or "").casefold()
    if any(tok in low_url for tok in ("linkedin.com/login", "linkedin.com/uas/login", "linkedin.com/authwall", "linkedin.com/checkpoint")):
        return True
    if "linkedin.com" not in low_url:
        return False
    if _has_easy_apply_trigger(page) is not None:
        return False
    try:
        if page.locator("input#username, input[name='session_key'], form[action*='login']").count() > 0:
            return True
    except Exception:
        pass
    return False


def _run_linkedin_easy_apply_assistant(
    page: Any,
    *,
    job: EnrichedJob,
    kit: dict[str, Any],
    profile: dict[str, Any],
    values: dict[str, str],
    cv_path: Path | None,
) -> dict[str, Any]:
    """Assistente passo-a-passo para Easy Apply: preenche cada etapa e PARA antes de Submeter."""
    trigger = _has_easy_apply_trigger(page)
    if trigger is None:
        return {"triggered": False}

    trigger.click(timeout=2500)
    page.wait_for_timeout(700)

    filled_all: list[str] = []
    cv_attached = False
    stopped_before_submit = False
    captcha_detected = False
    steps_completed = 0

    for _step in range(6):
        if _detect_captcha(page):
            captcha_detected = True
            break

        dialog = page.locator("[role='dialog'], .jobs-easy-apply-modal").first
        target_ctx = dialog if dialog.count() > 0 else page

        f_names, got_cv, open_qs = _fill_frame_inputs(
            page,
            values,
            cv_path if not cv_attached else None,
            profile,
        )
        filled_all.extend(f_names)
        if got_cv:
            cv_attached = True
        if open_qs:
            q_filled = _fill_pending_questions(
                page,
                [(page, idx, qtxt) for idx, qtxt in open_qs],
                job=job,
                kit=kit,
                profile=profile,
                cv_path=cv_path,
            )
            filled_all.extend(q_filled)
        steps_completed += 1

        buttons = target_ctx.locator("button")
        has_submit_btn = False
        next_btn = None
        for i in range(min(buttons.count(), 35)):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible() or not btn.is_enabled():
                    continue
                b_txt = re.sub(r"\s+", " ", (btn.inner_text(timeout=300) or "")).strip()
            except Exception:
                continue
            if not b_txt or len(b_txt) > 50:
                continue
            if SUBMIT_BLOCK_RE.search(b_txt):
                has_submit_btn = True
                break
            if next_btn is None and NEXT_STEP_RE.match(b_txt):
                next_btn = btn

        if has_submit_btn:
            stopped_before_submit = True
            break
        if next_btn is None:
            break

        prev_html = ""
        try:
            prev_html = target_ctx.inner_html(timeout=600)
        except Exception:
            pass
        try:
            next_btn.click(timeout=2000)
            page.wait_for_timeout(700)
        except Exception:
            break

        try:
            if target_ctx.locator(".artdeco-inline-feedback--error, [data-test-form-element-error-messages]").count() > 0:
                break
            if prev_html and target_ctx.inner_html(timeout=600) == prev_html:
                break
        except Exception:
            pass

    return {
        "triggered": True,
        "filled_fields": filled_all,
        "cv_attached": cv_attached,
        "stopped_before_submit": stopped_before_submit,
        "captcha_detected": captcha_detected,
        "steps_completed": steps_completed,
    }


def _inject_copilot_banner(page: Any, summary: str, *, tone: str = "info") -> None:
    border = "#f59e0b" if tone == "warn" else "#38bdf8"
    try:
        page.evaluate(
            """([msg, borderColor]) => {
                const existing = document.getElementById('estagio-radar-copilot-banner');
                if (existing) existing.remove();
                const bar = document.createElement('div');
                bar.id = 'estagio-radar-copilot-banner';
                bar.style.cssText = `position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#0f172a;color:#f8fafc;border-bottom:2px solid ${borderColor};padding:10px 16px;font:600 13px/1.4 system-ui,sans-serif;display:flex;justify-content:space-between;align-items:center;box-shadow:0 4px 16px rgba(0,0,0,0.35)`;
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
            [summary, border],
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
    """Abre o perfil Chrome dedicado para iniciar sessão 1 vez no LinkedIn / portais."""
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
        cdp_url = _ensure_cdp_chrome(prof_dir, CDP_PORT) if user_data_dir is None else None
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.bring_to_front()
            except Exception:
                pass
            page.goto(targets[0], wait_until="domcontentloaded", timeout=20000)
            _inject_copilot_banner(
                page,
                "Chrome dedicado Estágio Radar: inicia sessão no LinkedIn / portais (isolado dos teus outros 20 perfis e guardado para todos os auto-preenchimentos).",
            )
            for extra in targets[1:]:
                tab = context.new_page()
                tab.goto(extra, wait_until="domcontentloaded", timeout=20000)
            return

        context = _launch_persistent(p, prof_dir, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(targets[0], wait_until="domcontentloaded", timeout=20000)
            _inject_copilot_banner(
                page,
                "Chrome dedicado Estágio Radar: inicia sessão no LinkedIn / portais (fica guardado para os auto-preenchimentos). Fecha o browser quando terminares.",
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
    status_path: Path | None = AUTOFILL_STATUS_PATH,
) -> dict[str, Any]:
    """Abre a vaga no Chrome dedicado, fecha cookies, pré-preenche ATS/Easy Apply e anexa CV sem submeter."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError(
            "Playwright não instalado neste ambiente. Instala com: uv pip install playwright"
        ) from err

    url = target_url or kit.get("external_apply_url") or job.job_url
    cv_raw = str((kit.get("cv") or {}).get("path") or "").strip()
    cv_path = Path(cv_raw) if cv_raw else None
    cv_name = cv_path.name if cv_path else ""
    values = _build_field_values(profile, kit)

    track_status = status_path if not headless else None
    if track_status is not None:
        write_autofill_status(
            {
                "status": "running",
                "company": job.company,
                "title": job.title,
                "url": url,
                "cv_filename": cv_name,
                "message": f"A abrir {job.company} no Chrome dedicado e preencher campos...",
            },
            track_status,
        )

    filled_all: list[str] = []
    cv_attached = False
    final_url = url
    captcha_detected = False
    needs_login = False
    stopped_before_submit = False
    steps_completed = 0

    temp_ctx = tempfile.TemporaryDirectory() if (headless and user_data_dir is None) else None
    prof_dir = user_data_dir or (Path(temp_ctx.name) if temp_ctx is not None else CHROME_PROFILE_DIR)
    using_cdp = False

    try:
        with sync_playwright() as p:
            cdp_url = _ensure_cdp_chrome(prof_dir, CDP_PORT) if (not headless and user_data_dir is None) else None
            if cdp_url:
                using_cdp = True
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            else:
                context = _launch_persistent(p, prof_dir, headless=headless)
                page = context.pages[0] if context.pages else context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=22000)
                page.wait_for_timeout(900)
                _dismiss_cookie_banners(page)

                if _detect_captcha(page):
                    captcha_detected = True
                    _inject_copilot_banner(
                        page,
                        "⚠️ Verificação anti-bot/CAPTCHA detetada — completa manualmente na janela antes de continuar.",
                        tone="warn",
                    )
                elif _is_linkedin_login_wall(page):
                    needs_login = True
                    _inject_copilot_banner(
                        page,
                        "🔐 Inicia sessão no LinkedIn 1 vez neste Chrome dedicado — fica guardado para sempre!",
                        tone="warn",
                    )
                else:
                    easy_res = {"triggered": False}
                    if "linkedin.com" in page.url.casefold() or _has_easy_apply_trigger(page) is not None:
                        easy_res = _run_linkedin_easy_apply_assistant(
                            page,
                            job=job,
                            kit=kit,
                            profile=profile,
                            values=values,
                            cv_path=cv_path,
                        )

                    if easy_res.get("triggered"):
                        filled_all.extend(easy_res.get("filled_fields") or [])
                        cv_attached = bool(easy_res.get("cv_attached"))
                        stopped_before_submit = bool(easy_res.get("stopped_before_submit"))
                        captcha_detected = bool(easy_res.get("captcha_detected"))
                        steps_completed = int(easy_res.get("steps_completed") or 0)
                    else:
                        _maybe_open_apply_view(page)
                        if _detect_captcha(page):
                            captcha_detected = True
                        else:
                            frames_to_scan = [page.main_frame]
                            for fr in page.frames:
                                if fr == page.main_frame:
                                    continue
                                if ATS_FRAME_HINT_RE.search(fr.url or "") or fr.url.startswith("file://"):
                                    frames_to_scan.append(fr)

                            pending_questions: list[tuple[Any, int, str]] = []
                            for fr in frames_to_scan:
                                f_names, got_cv, open_qs = _fill_frame_inputs(
                                    fr,
                                    values,
                                    cv_path if not cv_attached else None,
                                    profile,
                                )
                                filled_all.extend(f_names)
                                if got_cv:
                                    cv_attached = True
                                for idx, qtxt in open_qs:
                                    pending_questions.append((fr, idx, qtxt))

                            if pending_questions:
                                filled_all.extend(
                                    _fill_pending_questions(
                                        page,
                                        pending_questions,
                                        job=job,
                                        kit=kit,
                                        profile=profile,
                                        cv_path=cv_path,
                                    )
                                )

                    unique_fields = list(dict.fromkeys(filled_all))
                    if captcha_detected:
                        _inject_copilot_banner(
                            page,
                            "⚠️ Verificação anti-bot/CAPTCHA detetada — completa manualmente na janela para continuar.",
                            tone="warn",
                        )
                    else:
                        banner_msg = (
                            f"Estágio Radar Copilot: {len(unique_fields)} campo(s) ({', '.join(unique_fields[:6])})"
                            + (f" + CV anexado ({cv_name})" if cv_attached else f" (anexa manualmente: {cv_name or 'CV PDF'})")
                            + " — Parado antes de Submeter. Revê e clica para enviar!"
                        )
                        _inject_copilot_banner(page, banner_msg)

                final_url = page.url

                if not using_cdp and keep_open and not headless:
                    try:
                        page.wait_for_event("close", timeout=0)
                    except Exception:
                        pass
            finally:
                if not using_cdp:
                    try:
                        context.close()
                    except Exception:
                        pass
    except Exception as err:
        if track_status is not None:
            write_autofill_status(
                {
                    "status": "error",
                    "company": job.company,
                    "title": job.title,
                    "url": final_url,
                    "error": str(err),
                    "message": f"Erro ao auto-preencher: {err}",
                },
                track_status,
            )
        raise
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()

    unique_fields = list(dict.fromkeys(filled_all))
    if captcha_detected:
        status_code = "captcha_blocked"
        msg = "⚠️ Verificação CAPTCHA/anti-bot detetada na página — completa manualmente no Chrome."
    elif needs_login:
        status_code = "needs_login"
        msg = "🔐 Faz login 1 vez no LinkedIn na janela que abriu — fica guardado para sempre."
    else:
        status_code = "done"
        cv_txt = f" + CV {cv_name} anexado!" if cv_attached else f" (anexa manualmente: {cv_name or 'CV'})"
        msg = f"✅ {len(unique_fields)} campo(s) preenchidos{cv_txt} Revê e clica em Submeter."

    result = {
        "ok": True,
        "status": status_code,
        "company": job.company,
        "title": job.title,
        "filled_fields": unique_fields,
        "cv_attached": cv_attached,
        "cv_filename": cv_name,
        "url": final_url,
        "captcha_detected": captcha_detected,
        "needs_login": needs_login,
        "stopped_before_submit": stopped_before_submit,
        "steps_completed": steps_completed,
        "message": msg,
    }
    if track_status is not None:
        write_autofill_status(result, track_status)
    return result
