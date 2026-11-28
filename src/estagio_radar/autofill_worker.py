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
from urllib.parse import parse_qs, urlparse

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
APPLY_NAV_RE = re.compile(
    r"\b(?:apply(?:\s+(?:now|for\s+this\s+(?:role|job|position)|online|externally|on\s+company(?:\s+site)?))?|candidatar(?:-me|-se)?|candidate-se|start\s+application|iniciar\s+candidatura|prosseguir\s+para\s+candidatura)\b",
    re.IGNORECASE,
)
APPLY_SKIP_RE = re.compile(
    r"\b(?:clicou\s+em|anunciar|post\s+a\s+job|sign\s+up|log\s+in|iniciar\s+sess|premium|alerta|alert|privacy|privacidade|cookies?)\b",
    re.IGNORECASE,
)
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


def _ensure_cdp_chrome(
    profile_dir: Path,
    port: int = CDP_PORT,
    *,
    initial_url: str = "about:blank",
) -> str | None:
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
        "--start-maximized",
        initial_url or "about:blank",
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
    if "recaptcha" in low or "h-captcha" in low or "turnstile" in low:
        return None
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
    """Deteta CAPTCHAs bloqueantes visíveis sem falsos positivos em badges invisíveis e sem nunca contornar."""
    frames = [page.main_frame, *[fr for fr in page.frames if fr != page.main_frame]]
    for fr in frames:
        try:
            has_blocking_widget = fr.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) return false;
                        const st = getComputedStyle(el);
                        return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
                    };
                    const iframes = document.querySelectorAll(
                        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], iframe[title*='recaptcha' i], iframe[title*='hcaptcha' i]"
                    );
                    for (const ifr of iframes) {
                        if (!isVisible(ifr)) continue;
                        const src = (ifr.getAttribute("src") || "").toLowerCase();
                        const title = (ifr.getAttribute("title") || "").toLowerCase();
                        if (src.includes("recaptcha")) {
                            if (src.includes("size=invisible") || ifr.closest(".grecaptcha-badge")) continue;
                        }
                        if (src.includes("recaptcha") || src.includes("hcaptcha") || src.includes("turnstile") || src.includes("challenges.cloudflare.com") || title.includes("recaptcha") || title.includes("hcaptcha")) {
                            return true;
                        }
                    }
                    const widgets = document.querySelectorAll(".h-captcha, .cf-turnstile, .g-recaptcha");
                    for (const w of widgets) {
                        if (!isVisible(w)) continue;
                        if (w.classList.contains("g-recaptcha") && (w.getAttribute("data-size") || "").toLowerCase() === "invisible") {
                            continue;
                        }
                        return true;
                    }
                    const els = document.querySelectorAll("h1, h2, p, label, div[class*='challenge' i], div[class*='captcha' i]");
                    for (const el of els) {
                        if (!isVisible(el)) continue;
                        const txt = (el.innerText || "").trim();
                        if (txt && txt.length <= 220 && /(verify you are human|confirm you are human|não sou um robô|i['’]?m not a robot)/i.test(txt)) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if has_blocking_widget:
                return True
        except Exception:
            pass
    return False


def _has_unsolved_captcha(page: Any) -> bool:
    """Devolve True apenas se existir CAPTCHA visível ainda sem token resolvido."""
    if not _detect_captcha(page):
        return False
    frames = [page.main_frame, *[fr for fr in page.frames if fr != page.main_frame]]
    for fr in frames:
        try:
            is_solved = fr.evaluate(
                """() => {
                    const sels = [
                        "textarea[name='g-recaptcha-response']",
                        "input[name='cf-turnstile-response']",
                        "textarea[name='h-captcha-response']"
                    ];
                    for (const s of sels) {
                        for (const el of document.querySelectorAll(s)) {
                            if ((el.value || "").trim().length > 20) return true;
                        }
                    }
                    return false;
                }"""
            )
            if is_solved:
                return False
        except Exception:
            continue
    return True


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


def _fill_custom_button_dropdowns(frame: Any, profile: dict[str, Any]) -> list[str]:
    """Preenche seletores customizados (<input type='button'> / button[aria-haspopup]) estilo Revolut/React."""
    filled: list[str] = []
    try:
        triggers = frame.eval_on_selector_all(
            "input[type='button'], button[aria-haspopup], [role='button'][aria-expanded]",
            """els => els.map((el, idx) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden";
                const cont = el.closest("div, fieldset, section, label");
                const parentTxt = (cont?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 140);
                return {
                    btnIdx: idx,
                    aria: el.getAttribute("aria-label") || "",
                    value: el.value || "",
                    text: (el.innerText || "").replace(/\\s+/g, " ").trim(),
                    parentText: parentTxt,
                    disabled: Boolean(el.disabled),
                    visible
                };
            })""",
        )
    except Exception:
        return filled

    locator_all = frame.locator("input[type='button'], button[aria-haspopup], [role='button'][aria-expanded]")
    for meta in triggers:
        if meta.get("disabled") or not meta.get("visible"):
            continue
        desc = f"{meta.get('aria', '')} | {meta.get('parentText', '')}".casefold()
        if any(k in desc for k in ("phone country code", "country code", "indicativo")):
            tokens = ["portugal", "+351"]
            field_tag = "phone_country"
        elif any(k in desc for k in ("current country", "country of residence", "country")) and "phone" not in desc:
            cur = f"{meta.get('value', '')} {meta.get('text', '')}".casefold()
            if "portugal" in cur:
                continue
            tokens = [str(profile.get("country") or "Portugal"), "portugal"]
            field_tag = "country"
        else:
            continue

        try:
            locator_all.nth(meta["btnIdx"]).click(timeout=1200)
            if hasattr(frame, "wait_for_timeout"):
                frame.wait_for_timeout(250)
            search_inp = frame.locator(
                "[role='dialog'] input[type='text'], [role='dialog'] input[type='search'], "
                "[role='listbox'] input, input[placeholder*='search' i], input[aria-label*='search' i]"
            ).first
            if search_inp.count() > 0 and search_inp.is_visible():
                search_inp.fill("Portugal", timeout=1000)
                if hasattr(frame, "wait_for_timeout"):
                    frame.wait_for_timeout(250)

            opts = frame.locator("[role='option'], [role='listbox'] li, [role='listbox'] button, [role='dialog'] li, [role='dialog'] button")
            matched = False
            for tok in tokens:
                for i in range(min(opts.count(), 40)):
                    opt = opts.nth(i)
                    try:
                        if not opt.is_visible():
                            continue
                        o_txt = (opt.inner_text(timeout=200) or "").strip().casefold()
                    except Exception:
                        continue
                    if tok.casefold() in o_txt and len(o_txt) <= 80:
                        opt.click(timeout=1200)
                        filled.append(field_tag)
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                try:
                    kb = getattr(frame, "keyboard", None) or getattr(getattr(frame, "page", None), "keyboard", None)
                    if kb is not None:
                        kb.press("Escape")
                except Exception:
                    pass
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

    custom_filled = _fill_custom_button_dropdowns(frame, profile)
    filled.extend(custom_filled)
    local_values = dict(values)
    if "phone_country" in custom_filled and local_values.get("phone"):
        local_values["phone"] = re.sub(r"^(?:\+351|00351)\s*", "", local_values["phone"]).strip()

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
        if kind and local_values.get(kind) and not str(meta.get("value") or "").strip():
            try:
                locator_all.nth(meta["idx"]).fill(local_values[kind], timeout=1500)
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


def _has_real_application_form(page: Any) -> bool:
    """Deteta se a página (ou iframe ATS) já mostra o formulário real de candidatura."""
    frames = [page.main_frame]
    for fr in page.frames:
        if fr != page.main_frame and (ATS_FRAME_HINT_RE.search(fr.url or "") or fr.url.startswith("file://")):
            frames.append(fr)

    for fr in frames:
        try:
            has_form = fr.evaluate(
                """() => {
                    const isVisible = (el) => {
                        let anc = el.parentElement;
                        while (anc && anc !== document.body && anc !== document.documentElement) {
                            const st = getComputedStyle(anc);
                            if (st.display === "none" || st.visibility === "hidden") return false;
                            anc = anc.parentElement;
                        }
                        if (el.type === "file") return true;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden";
                    };
                    let hasFile = false;
                    let hasPassword = false;
                    let matchedInputs = 0;
                    for (const el of document.querySelectorAll("input, textarea")) {
                        if (el.disabled || !isVisible(el)) continue;
                        const tp = (el.getAttribute("type") || "text").toLowerCase();
                        if (tp === "file") { hasFile = true; continue; }
                        if (tp === "password") { hasPassword = true; continue; }
                        if (["hidden", "checkbox", "radio", "submit", "button", "search"].includes(tp)) continue;
                        const desc = [
                            el.name, el.id, el.placeholder, el.getAttribute("aria-label"), el.getAttribute("autocomplete")
                        ].join(" ").toLowerCase();
                        if (/(?:full\\s*name|first\\s*name|last\\s*name|email|e-mail|phone|telem|linkedin|github|portfolio|cover|carta)/.test(desc) || tp === "email" || tp === "tel") {
                            matchedInputs += 1;
                        }
                    }
                    if (hasPassword && !hasFile) return false;
                    return hasFile || matchedInputs >= 2;
                }"""
            )
            if has_form:
                return True
        except Exception:
            continue
    return False


def _extract_linkedin_external_apply_url(page: Any) -> str | None:
    """Extrai o link direto da empresa dentro do botão 'Candidatar-se' do LinkedIn."""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(el => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== "hidden";
                return {
                    visible,
                    aria: el.getAttribute("aria-label") || "",
                    text: (el.innerText || "").replace(/\\s+/g, " ").trim(),
                    href: el.getAttribute("href") || ""
                };
            })""",
        )
    except Exception:
        return None

    candidates: list[tuple[int, str]] = []
    for item in hrefs:
        if not item.get("visible"):
            continue
        blob = f"{item.get('aria', '')} {item.get('text', '')}".strip()
        if not blob or len(blob) > 65:
            continue
        if APPLY_SKIP_RE.search(blob) or SUBMIT_BLOCK_RE.search(blob):
            continue
        if not APPLY_NAV_RE.search(blob):
            continue
        href = str(item.get("href") or "").strip()
        if not href:
            continue
        if "/safety/go/" in href or "/redir/redirect" in href:
            qs = parse_qs(urlparse(href).query)
            ext = (qs.get("url") or [""])[0].strip()
            if ext.startswith("http"):
                prio = 2 if re.search(r"empresa|company|external", blob, re.I) else 1
                candidates.append((prio, ext))
        elif href.startswith("http") and "linkedin.com" not in href.casefold():
            candidates.append((1, href))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _follow_apply_hops(page: Any, context: Any, *, max_hops: int = 4) -> Any:
    """Segue até max_hops camadas (mesmo separador ou novo separador) até alcançar o formulário."""
    for _hop in range(max_hops):
        _dismiss_cookie_banners(page)
        if _has_real_application_form(page):
            return page
        if _has_unsolved_captcha(page):
            return page

        low_url = (page.url or "").casefold()
        if "linkedin.com/jobs" in low_url:
            ext_url = _extract_linkedin_external_apply_url(page)
            if ext_url:
                try:
                    page.goto(ext_url, wait_until="domcontentloaded", timeout=22000)
                    page.wait_for_timeout(900)
                    continue
                except Exception:
                    pass

        if "jobs.lever.co" in low_url and not page.url.rstrip("/").endswith("/apply"):
            try:
                page.goto(page.url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(700)
                continue
            except Exception:
                pass

        try:
            clickable = page.locator(".jobs-apply-button, [data-testid*='apply' i], a, button")
            count = min(clickable.count(), 60)
        except Exception:
            break

        chosen = None
        for i in range(count):
            el = clickable.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = re.sub(r"\s+", " ", (el.inner_text(timeout=250) or "")).strip()
                aria = re.sub(r"\s+", " ", (el.get_attribute("aria-label") or "")).strip()
            except Exception:
                continue
            blob = f"{txt} {aria}".strip()
            if not blob or len(blob) > 65:
                continue
            if SUBMIT_BLOCK_RE.search(blob) or APPLY_SKIP_RE.search(blob):
                continue
            if APPLY_NAV_RE.search(blob):
                chosen = el
                break

        if chosen is None:
            break

        prev_pages = list(context.pages)
        prev_url = page.url
        try:
            chosen.click(timeout=2500)
        except Exception:
            break

        advanced = False
        for _tick in range(10):
            page.wait_for_timeout(250)
            new_tabs = [pg for pg in context.pages if pg not in prev_pages]
            if new_tabs:
                page = new_tabs[-1]
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=18000)
                except Exception:
                    pass
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                page.wait_for_timeout(800)
                advanced = True
                break
            if page.url != prev_url:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                advanced = True
                break
            if _has_real_application_form(page):
                advanced = True
                break

        if not advanced:
            break
    return page


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


def open_radar_app(
    dashboard_url: str = "http://127.0.0.1:8765",
    *,
    user_data_dir: Path | None = None,
    port: int = CDP_PORT,
) -> dict[str, Any]:
    """Abre ou foca o Dashboard no Chrome dedicado isolado (sem menu de perfis)."""
    prof_dir = user_data_dir or CHROME_PROFILE_DIR
    clean_base = dashboard_url.rstrip("/")

    if not _cdp_endpoint_ready(port):
        endpoint = _ensure_cdp_chrome(prof_dir, port, initial_url=dashboard_url)
        if endpoint:
            return {"ok": True, "mode": "launched_cdp", "url": dashboard_url}
        import webbrowser

        webbrowser.open(dashboard_url)
        return {"ok": True, "mode": "fallback_webbrowser", "url": dashboard_url}

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            for existing in context.pages:
                if (existing.url or "").rstrip("/").startswith(clean_base):
                    try:
                        existing.bring_to_front()
                    except Exception:
                        pass
                    return {"ok": True, "mode": "focused_existing", "url": existing.url}
            page = context.new_page()
            page.goto(dashboard_url, wait_until="domcontentloaded", timeout=15000)
            try:
                page.bring_to_front()
            except Exception:
                pass
            return {"ok": True, "mode": "opened_tab", "url": dashboard_url}
    except Exception as err:
        logger.warning("Fallback webbrowser ao abrir Dashboard: %s", err)
        import webbrowser

        webbrowser.open(dashboard_url)
        return {"ok": True, "mode": "fallback_webbrowser", "url": dashboard_url}


_GENERIC_COMPANY_WORDS = {
    "company", "portugal", "group", "solutions", "technologies", "services",
    "software", "systems", "labs", "tech", "limited", "inc", "lda", "sa", "the",
}


def _get_or_open_page(context: Any) -> Any:
    """Reutiliza separador about:blank vazio antes de criar um novo separador."""
    for pg in context.pages:
        url = (getattr(pg, "url", "") or "").strip()
        if url in ("", "about:blank"):
            return pg
    return context.new_page()


def _find_matching_open_tab(
    pages: list[Any],
    job: EnrichedJob,
    target_url: str,
    dashboard_port: int = 8765,
) -> Any | None:
    """Deteta separador já aberto desta vaga/empresa para preencher no sítio sem recarregar."""
    dash_prefixes = (f"http://127.0.0.1:{dashboard_port}", f"http://localhost:{dashboard_port}")

    raw_comp = (job.company or "").casefold()
    comp_slug = re.sub(r"[^a-z0-9]+", "", raw_comp)
    comp_tokens = {
        w for w in re.split(r"[^a-z0-9]+", raw_comp)
        if len(w) >= 4 and w not in _GENERIC_COMPANY_WORDS
    }
    if len(comp_slug) >= 4:
        comp_tokens.add(comp_slug)

    li_job_ids: set[str] = set()
    for u in (target_url or "", job.job_url or ""):
        m = re.search(r"(\d{8,12})", u)
        if m:
            li_job_ids.add(m.group(1))

    target_hosts: set[str] = set()
    for u in (target_url or "", job.job_url or ""):
        host = (urlparse(u).netloc or "").casefold().removeprefix("www.")
        if host and "linkedin.com" not in host:
            target_hosts.add(host)

    candidates: list[tuple[int, Any]] = []
    for pg in pages:
        url = (getattr(pg, "url", "") or "").strip()
        low = url.casefold()
        if not low or low.startswith(("about:blank", "chrome://", "devtools://", *dash_prefixes)):
            continue
        if "linkedin.com" in low:
            if any(jid in low for jid in li_job_ids):
                candidates.append((2, pg))
            continue
        pg_host = (urlparse(low).netloc or "").removeprefix("www.")
        if (pg_host and pg_host in target_hosts) or any(tok in low for tok in comp_tokens):
            candidates.append((3, pg))

    if not candidates:
        return None
    max_prio = max(p for p, _ in candidates)
    matching = [pg for p, pg in candidates if p == max_prio]
    return matching[-1]


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
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=12000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = _get_or_open_page(context)
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


def _fill_current_page(
    page: Any,
    context: Any,
    *,
    job: EnrichedJob,
    kit: dict[str, Any],
    profile: dict[str, Any],
    cv_path: Path | None,
    values: dict[str, str],
    follow_hops: bool = True,
) -> dict[str, Any]:
    """Executa deteção de CAPTCHA/login, saltos multi-camada, Easy Apply e preenchimento de formulário."""
    cv_name = cv_path.name if cv_path else ""
    filled_all: list[str] = []
    cv_attached = False
    captcha_detected = False
    blocking_captcha = False
    needs_login = False
    stopped_before_submit = False
    steps_completed = 0

    _dismiss_cookie_banners(page)
    has_form = _has_real_application_form(page)

    if not has_form and _has_unsolved_captcha(page):
        blocking_captcha = True
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
        if "linkedin.com" in (page.url or "").casefold() or _has_easy_apply_trigger(page) is not None:
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
            blocking_captcha = captcha_detected
            steps_completed = int(easy_res.get("steps_completed") or 0)
        else:
            if follow_hops and not has_form:
                page = _follow_apply_hops(page, context, max_hops=4)
                has_form = _has_real_application_form(page)
            if not has_form and _has_unsolved_captcha(page):
                blocking_captcha = True
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
                captcha_detected = _has_unsolved_captcha(page)

        unique_fields = list(dict.fromkeys(filled_all))
        if blocking_captcha:
            _inject_copilot_banner(
                page,
                "⚠️ Verificação anti-bot/CAPTCHA detetada — completa manualmente na janela para continuar.",
                tone="warn",
            )
        else:
            tail_txt = (
                " — Marca o CAPTCHA em baixo e clica em Submeter!"
                if captcha_detected
                else " — Parado antes de Submeter. Revê e clica para enviar!"
            )
            banner_msg = (
                f"Estágio Radar Copilot: {len(unique_fields)} campo(s) ({', '.join(unique_fields[:6])})"
                + (f" + CV anexado ({cv_name})" if cv_attached else f" (anexa manualmente: {cv_name or 'CV PDF'})")
                + tail_txt
            )
            _inject_copilot_banner(page, banner_msg)

    unique_fields = list(dict.fromkeys(filled_all))
    if blocking_captcha:
        status_code = "captcha_blocked"
        msg = "⚠️ Verificação CAPTCHA/anti-bot detetada na página — completa manualmente no Chrome."
    elif needs_login:
        status_code = "needs_login"
        msg = "🔐 Faz login 1 vez no LinkedIn na janela que abriu — fica guardado para sempre."
    else:
        status_code = "done"
        cv_txt = f" + CV {cv_name} anexado!" if cv_attached else f" (anexa manualmente: {cv_name or 'CV'})"
        if captcha_detected:
            msg = f"✅ {len(unique_fields)} campo(s) preenchidos{cv_txt} Só falta marcar o CAPTCHA em baixo e clicar em Submeter."
        else:
            msg = f"✅ {len(unique_fields)} campo(s) preenchidos{cv_txt} Revê e clica em Submeter."

    return {
        "ok": True,
        "status": status_code,
        "company": job.company,
        "title": job.title,
        "filled_fields": unique_fields,
        "cv_attached": cv_attached,
        "cv_filename": cv_name,
        "url": page.url,
        "captcha_detected": captcha_detected,
        "needs_login": needs_login,
        "stopped_before_submit": stopped_before_submit,
        "steps_completed": steps_completed,
        "message": msg,
    }


def _pick_active_application_tab(pages: list[Any], dashboard_port: int = 8765) -> Any | None:
    """Seleciona o separador aberto do formulário (prioriza sites externos vs LinkedIn/Dashboard)."""
    candidates: list[tuple[int, Any]] = []
    dash_prefixes = (f"http://127.0.0.1:{dashboard_port}", f"http://localhost:{dashboard_port}")
    for pg in pages:
        url = (getattr(pg, "url", "") or "").strip()
        low = url.casefold()
        if not low or low.startswith(("about:blank", "chrome://", "devtools://", *dash_prefixes)):
            continue
        prio = 1 if "linkedin.com" in low else 2
        candidates.append((prio, pg))
    if not candidates:
        return None
    max_prio = max(p for p, _ in candidates)
    matching = [pg for p, pg in candidates if p == max_prio]
    return matching[-1]


def run_autofill_active_tab(
    job: EnrichedJob,
    kit: dict[str, Any],
    profile: dict[str, Any],
    *,
    port: int = CDP_PORT,
    status_path: Path | None = AUTOFILL_STATUS_PATH,
) -> dict[str, Any]:
    """Preenche o separador externo que o utilizador já tem aberto no Chrome dedicado."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:
        raise RuntimeError("Playwright não instalado neste ambiente.") from err

    cv_raw = str((kit.get("cv") or {}).get("path") or "").strip()
    cv_path = Path(cv_raw) if cv_raw else None
    cv_name = cv_path.name if cv_path else ""
    values = _build_field_values(profile, kit)

    if status_path is not None:
        write_autofill_status(
            {
                "status": "running",
                "company": job.company,
                "title": job.title,
                "cv_filename": cv_name,
                "message": f"A preencher separador aberto de {job.company}...",
            },
            status_path,
        )

    if not _cdp_endpoint_ready(port):
        err_res = {
            "ok": False,
            "status": "error",
            "message": "Abre primeiro o Chrome dedicado pelo atalho do Desktop ou botão Auto-Preencher.",
        }
        if status_path is not None:
            write_autofill_status(err_res, status_path)
        return err_res

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=12000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = _pick_active_application_tab(list(context.pages))
            if page is None:
                err_res = {
                    "ok": False,
                    "status": "error",
                    "message": "Nenhum separador de vaga aberto encontrado no Chrome dedicado.",
                }
                if status_path is not None:
                    write_autofill_status(err_res, status_path)
                return err_res

            try:
                page.bring_to_front()
            except Exception:
                pass
            result = _fill_current_page(
                page,
                context,
                job=job,
                kit=kit,
                profile=profile,
                cv_path=cv_path,
                values=values,
                follow_hops=True,
            )
            if status_path is not None:
                write_autofill_status(result, status_path)
            return result
    except Exception as err:
        if status_path is not None:
            write_autofill_status(
                {
                    "status": "error",
                    "company": job.company,
                    "title": job.title,
                    "error": str(err),
                    "message": f"Erro ao preencher separador aberto: {err}",
                },
                status_path,
            )
        raise


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
    """Abre a vaga no Chrome dedicado, segue saltos multi-camada e pré-preenche ATS/Easy Apply + CV."""
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

    result: dict[str, Any] = {}
    temp_ctx = tempfile.TemporaryDirectory() if (headless and user_data_dir is None) else None
    prof_dir = user_data_dir or (Path(temp_ctx.name) if temp_ctx is not None else CHROME_PROFILE_DIR)
    using_cdp = False

    try:
        with sync_playwright() as p:
            cdp_url = _ensure_cdp_chrome(prof_dir, CDP_PORT) if (not headless and user_data_dir is None) else None
            if cdp_url:
                using_cdp = True
                browser = p.chromium.connect_over_cdp(cdp_url, timeout=12000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                existing_tab = _find_matching_open_tab(list(context.pages), job, url)
                if existing_tab is not None:
                    page = existing_tab
                    navigated = False
                else:
                    page = _get_or_open_page(context)
                    navigated = True
                try:
                    page.bring_to_front()
                except Exception:
                    pass
            else:
                context = _launch_persistent(p, prof_dir, headless=headless)
                page = context.pages[0] if context.pages else context.new_page()
                navigated = True

            try:
                if navigated:
                    page.goto(url, wait_until="domcontentloaded", timeout=22000)
                    page.wait_for_timeout(900)
                result = _fill_current_page(
                    page,
                    context,
                    job=job,
                    kit=kit,
                    profile=profile,
                    cv_path=cv_path,
                    values=values,
                    follow_hops=True,
                )
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
                    "url": url,
                    "error": str(err),
                    "message": f"Erro ao auto-preencher: {err}",
                },
                track_status,
            )
        raise
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()

    if track_status is not None:
        write_autofill_status(result, track_status)
    return result
