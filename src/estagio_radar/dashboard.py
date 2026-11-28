"""Servidor web interativo local para consultar, filtrar e lançar candidaturas 1-clique no browser."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from estagio_radar.ai_writer import (
    answer_form_questions_with_gemini,
    load_cv_markdown,
    verify_gemini_api_key,
)
from estagio_radar.apply_copilot import (
    DEFAULT_PROFILE_PATH,
    generate_application_kit,
    list_available_cvs,
    load_candidate_profile,
    select_best_cv,
    send_application_email,
)
from estagio_radar.autofill_worker import read_autofill_status, write_autofill_status
from estagio_radar.cv_tailor import generate_tailored_cv_pdf
from estagio_radar.exporters import render_dashboard_html
from estagio_radar.models import CompanyProfile, DomainCategory, EnrichedJob, GeoZone, RoleTier, ScoreBreakdown
from estagio_radar.storage import RadarStorage

logger = logging.getLogger("estagio_radar.dashboard")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_python_exe() -> Path:
    py_exe = Path(sys.executable)
    if py_exe.name.lower() == "pythonw.exe":
        alt = py_exe.with_name("python.exe")
        if alt.exists():
            return alt
    return py_exe


def _write_env_setting(repo_root: Path, key: str, value: str) -> None:
    env_path = repo_root / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def _infer_company_and_domain(url: str, title: str, page_text: str) -> tuple[str, DomainCategory]:
    company = "Empresa"
    if " at " in title.lower():
        parts = title.rsplit(" at ", 1)
        company = parts[-1].split("|")[0].split("-")[0].strip() or company
    elif "greenhouse.io/" in url:
        company = url.split("greenhouse.io/")[1].split("/")[0].replace("-", " ").title()
    elif "jobs.smartrecruiters.com/" in url:
        company = url.split("jobs.smartrecruiters.com/")[1].split("/")[0]
    elif "ashbyhq.com/" in url:
        company = url.split("ashbyhq.com/")[1].split("/")[0].replace("-", " ").title()

    blob = f"{title} {page_text[:1000]}".casefold()
    if any(k in blob for k in ("pytorch", "jax", "machine learning", "deep learning", "llm", "generative ai", "artificial intelligence")):
        domain = DomainCategory.AI_ML
    elif any(k in blob for k in ("data science", "data analyst", "data engineer", "analytics", "power bi", "sql")):
        domain = DomainCategory.DATA
    else:
        domain = DomainCategory.SWE
    return company, domain


def make_handler(
    storage: RadarStorage,
    scan_callback: Callable[[], dict[str, Any]] | None = None,
    profile_path: str | Path = DEFAULT_PROFILE_PATH,
):
    scan_lock = threading.Lock()
    repo_root = _resolve_repo_root()
    resolved_profile_path = Path(profile_path)
    if not resolved_profile_path.is_absolute():
        resolved_profile_path = repo_root / resolved_profile_path

    class RadarDashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_pdf(self, pdf_path: Path) -> None:
            data = pdf_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{pdf_path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/" or path == "/index.html":
                jobs = storage.list_jobs(active_only=True)
                self._send_html(render_dashboard_html(jobs, interactive=True))
                return
            if path == "/api/jobs":
                jobs = [j.model_dump(mode="json") for j in storage.list_jobs(active_only=True)]
                self._send_json(HTTPStatus.OK, {"jobs": jobs, "count": len(jobs)})
                return
            if path == "/api/stats":
                self._send_json(HTTPStatus.OK, storage.get_stats())
                return
            if path == "/api/profile":
                profile = load_candidate_profile(resolved_profile_path)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "configured": bool(profile),
                        "first_name": profile.get("first_name", ""),
                        "last_name": profile.get("last_name", ""),
                        "full_name": profile.get("full_name", ""),
                        "email": profile.get("email", ""),
                        "phone": profile.get("phone", ""),
                        "location": profile.get("location", ""),
                        "university": profile.get("university", ""),
                        "degree": profile.get("degree", ""),
                        "graduation_year": profile.get("graduation_year", "2027"),
                        "gpa": profile.get("gpa", ""),
                        "linkedin_url": profile.get("linkedin_url", ""),
                        "github_url": profile.get("github_url", ""),
                        "portfolio_url": profile.get("portfolio_url", ""),
                        "gemini_configured": bool((os.getenv("GEMINI_API_KEY") or "").strip()),
                        "gemini_model": (os.getenv("GEMINI_MODEL") or "gemini-4-hydrogen").strip(),
                        "cvs": list_available_cvs(profile, repo_root=repo_root),
                    },
                )
                return
            if path == "/api/cv-file":
                fp = (qs.get("fingerprint") or [""])[0].strip()
                track = (qs.get("track") or [""])[0].strip()
                if fp:
                    job = storage.get_job(fp)
                    if job is not None and job.tailored_cv_path and Path(job.tailored_cv_path).exists():
                        self._send_pdf(Path(job.tailored_cv_path))
                        return
                if track and track not in {"ai_ml", "swe", "enterprise"}:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "track inválido (usar ai_ml, swe ou enterprise)"})
                    return
                if not track:
                    track = "ai_ml"
                profile = load_candidate_profile(resolved_profile_path)
                cv_meta = next((c for c in list_available_cvs(profile, repo_root=repo_root) if c["track"] == track), None)
                if not cv_meta or not cv_meta["exists"]:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ficheiro CV PDF não encontrado"})
                    return
                self._send_pdf(Path(cv_meta["path"]))
                return
            if path == "/api/followups":
                due = [j.model_dump(mode="json") for j in storage.list_due_followups()]
                self._send_json(HTTPStatus.OK, {"count": len(due), "jobs": due})
                return
            if path == "/api/apply-kit":
                fp = (qs.get("fingerprint") or [""])[0].strip()
                cv_track = (qs.get("cv_track") or [""])[0].strip() or None
                tone = (qs.get("tone") or ["balanced"])[0].strip() or "balanced"
                refresh = (qs.get("refresh") or ["0"])[0].strip() == "1"
                if not fp:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "fingerprint obrigatório"})
                    return
                job = storage.get_job(fp)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Vaga não encontrada"})
                    return
                profile = load_candidate_profile(resolved_profile_path)
                kit = generate_application_kit(
                    job,
                    profile,
                    override_track=cv_track,
                    fetch_email=True,
                    repo_root=repo_root,
                    storage=storage,
                    tone=tone,
                    force_refresh=refresh,
                )
                self._send_json(HTTPStatus.OK, kit)
                return
            if path == "/api/autofill-status":
                self._send_json(HTTPStatus.OK, read_autofill_status(repo_root / "data/last_autofill.json"))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint não encontrado"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON inválido"})
                return

            if path == "/api/status":
                fp = str(data.get("fingerprint") or "").strip()
                status = str(data.get("status") or "").strip()
                notes = data.get("notes")
                recruiter_contact = data.get("recruiter_contact")
                try:
                    updated = storage.update_job_status(
                        fp,
                        status,
                        str(notes) if notes is not None else None,
                        recruiter_contact=str(recruiter_contact) if recruiter_contact is not None else None,
                    )
                except ValueError as err:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
                    return
                refreshed = storage.get_job(fp) if updated else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": updated,
                        "applied_at": refreshed.applied_at.isoformat() if refreshed and refreshed.applied_at else None,
                        "followup_at": refreshed.followup_at.isoformat() if refreshed and refreshed.followup_at else None,
                    },
                )
                return

            if path == "/api/followup-done":
                fp = str(data.get("fingerprint") or "").strip()
                snooze_days = int(data.get("snooze_days") or 7)
                note = data.get("note")
                ok = storage.mark_followup_done(
                    fp,
                    snooze_days=snooze_days,
                    note=str(note) if note is not None else None,
                )
                refreshed = storage.get_job(fp) if ok else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": ok,
                        "followup_at": refreshed.followup_at.isoformat() if refreshed and refreshed.followup_at else None,
                    },
                )
                return

            if path == "/api/tailor-cv":
                fp = str(data.get("fingerprint") or "").strip()
                cv_track = str(data.get("cv_track") or "").strip() or None
                job = storage.get_job(fp)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Vaga não encontrada"})
                    return
                profile = load_candidate_profile(resolved_profile_path)
                base_cv = select_best_cv(job, profile, override_track=cv_track, repo_root=repo_root)
                if not base_cv["exists"]:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "CV base PDF em falta para adaptar."})
                    return
                details = storage.get_job_details(fp)
                description = (details.get("full_description") or job.description_snippet or "").strip()
                try:
                    res = generate_tailored_cv_pdf(
                        job,
                        base_cv_pdf=Path(base_cv["path"]),
                        description=description,
                        profile=profile,
                        out_dir=repo_root / "data/tailored_cvs",
                    )
                    storage.save_tailored_cv(fp, res["path"])
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "cv": {
                                "track": base_cv["track"],
                                "label": f"CV Adaptado ({res['filename']})",
                                "path": res["path"],
                                "filename": res["filename"],
                                "exists": True,
                                "tailored": True,
                                "reason": f"Adaptado com {res['model']} ({len(res['matched_keywords'])} keywords)",
                            },
                            "matched_keywords": res["matched_keywords"],
                            "headline": res["headline"],
                            "model": res["model"],
                            "preview_url": f"/api/cv-file?fingerprint={fp}",
                        },
                    )
                except Exception as err:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Falha ao adaptar CV: {err}"})
                return

            if path == "/api/answer-questions":
                company = str(data.get("company") or "Empresa").strip()
                title = str(data.get("title") or "").strip()
                page_text = str(data.get("page_text") or "")
                questions = [str(q) for q in (data.get("questions") or []) if str(q).strip()]
                cv_track = str(data.get("cv_track") or "swe").strip()
                profile = load_candidate_profile(resolved_profile_path)
                cv_meta = next((c for c in list_available_cvs(profile, repo_root=repo_root) if c["track"] == cv_track), None)
                cv_md = load_cv_markdown(cv_meta["path"]) if cv_meta else ""
                lang = "pt" if any(w in (title + " " + " ".join(questions)).lower() for w in ("estág", "estag", "porqu", "motiv", "bolsa")) else "en"
                answers = answer_form_questions_with_gemini(
                    questions=questions,
                    company=company,
                    title=title,
                    page_text=page_text,
                    cv_markdown=cv_md,
                    profile=profile,
                    language=lang,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "answers": answers})
                return

            if path == "/api/generate-for-page":
                url = str(data.get("url") or "https://example.com").strip()
                title = str(data.get("title") or "Internship").strip()
                page_text = str(data.get("page_text") or "").strip()
                cv_track = str(data.get("cv_track") or "").strip() or None
                tone = str(data.get("tone") or "balanced").strip() or "balanced"
                company, domain = _infer_company_and_domain(url, title, page_text)
                profile = load_candidate_profile(resolved_profile_path)
                temp_job = EnrichedJob(
                    fingerprint="page-live",
                    title=title,
                    company=company,
                    location="Portugal",
                    job_url=url,
                    canonical_url=url,
                    sources=["Chrome Extension"],
                    primary_source="Chrome Extension",
                    source_type="ats",
                    role_tier=RoleTier.INTERNSHIP,
                    domain=domain,
                    geo_zone=GeoZone.LISBOA_METRO,
                    description_snippet=page_text[:4000],
                    company_profile=CompanyProfile(company_name=company, normalized_name=company.lower()),
                    score=80.0,
                    score_breakdown=ScoreBreakdown(),
                )
                kit = generate_application_kit(
                    temp_job,
                    profile,
                    override_track=cv_track,
                    fetch_email=False,
                    repo_root=repo_root,
                    tone=tone,
                )
                self._send_json(HTTPStatus.OK, kit)
                return

            if path == "/api/browser-login":
                py_exe = _resolve_python_exe()
                cmd = [str(py_exe), "-m", "estagio_radar", "browser-login"]
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                log_path = repo_root / "data/autofill.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(log_path, "a", encoding="utf-8") as log_fh:
                        subprocess.Popen(
                            cmd,
                            cwd=str(repo_root),
                            stdin=subprocess.DEVNULL,
                            stdout=log_fh,
                            stderr=log_fh,
                            creationflags=creationflags,
                        )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "message": "Chrome dedicado aberto — inicia sessão 1x no LinkedIn (nunca toca nos teus outros perfis!).",
                        },
                    )
                except Exception as err:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Falha ao abrir Chrome dedicado: {err}"})
                return

            if path == "/api/settings/gemini-key":
                api_key = str(data.get("api_key") or "").strip()
                if not api_key:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "API Key vazia."})
                    return
                ok, model_used, msg = verify_gemini_api_key(api_key)
                if not ok:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "gemini_configured": False, "error": msg})
                    return
                _write_env_setting(repo_root, "GEMINI_API_KEY", api_key)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "gemini_configured": True,
                        "model": model_used,
                        "message": msg,
                    },
                )
                return

            if path in {"/api/autofill", "/api/autofill-active-tab"}:
                active_tab_only = path == "/api/autofill-active-tab"
                fp = str(data.get("fingerprint") or "").strip()
                payload_dir = storage.db_path.parent if storage.db_path.is_absolute() else (repo_root / "data")
                payload_file = payload_dir / "last_autofill_payload.json"
                if active_tab_only and not fp and payload_file.exists():
                    try:
                        saved_payload = json.loads(payload_file.read_text(encoding="utf-8"))
                        fp = str(saved_payload.get("fingerprint") or "").strip()
                    except Exception:
                        fp = ""
                if not fp and (repo_root / "data/last_autofill_payload.json").exists() and active_tab_only:
                    try:
                        saved_payload = json.loads((repo_root / "data/last_autofill_payload.json").read_text(encoding="utf-8"))
                        fp = str(saved_payload.get("fingerprint") or "").strip()
                    except Exception:
                        fp = ""
                cv_track = str(data.get("cv_track") or "").strip() or None
                use_tailored = bool(data.get("use_tailored", True))
                cover_note = str(data.get("cover_note") or "").strip()
                language = str(data.get("language") or "en").strip() or "en"
                job = storage.get_job(fp)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Vaga não encontrada"})
                    return
                profile = load_candidate_profile(resolved_profile_path)
                cv_info = select_best_cv(
                    job,
                    profile,
                    override_track=cv_track,
                    repo_root=repo_root,
                )
                if use_tailored and job.tailored_cv_path and Path(job.tailored_cv_path).exists():
                    t_path = Path(job.tailored_cv_path)
                    cv_info = {
                        **cv_info,
                        "label": f"CV Adaptado ({t_path.name})",
                        "path": str(t_path),
                        "filename": t_path.name,
                        "exists": True,
                        "tailored": True,
                    }
                details = storage.get_job_details(fp)

                payload_file.parent.mkdir(parents=True, exist_ok=True)
                payload_file.write_text(
                    json.dumps(
                        {
                            "fingerprint": fp,
                            "cv": cv_info,
                            "external_apply_url": details.get("external_apply_url"),
                            "short_cover_note": cover_note,
                            "language": language,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                start_msg = (
                    f"A preencher separador aberto de {job.company} ({cv_info['filename']})..."
                    if active_tab_only
                    else f"A abrir Chrome dedicado para {job.company} ({cv_info['filename']})..."
                )
                status_file = payload_dir / "last_autofill.json"
                write_autofill_status(
                    {
                        "status": "starting",
                        "company": job.company,
                        "title": job.title,
                        "cv_filename": cv_info["filename"],
                        "message": start_msg,
                    },
                    status_file,
                )

                py_exe = _resolve_python_exe()
                cmd = [
                    str(py_exe),
                    "-u",
                    "-m",
                    "estagio_radar",
                    "autofill",
                    "--fingerprint",
                    fp,
                    "--db",
                    str(storage.db_path),
                    "--profile",
                    str(resolved_profile_path),
                    "--payload-file",
                    str(payload_file),
                    "--skip-ai",
                ]
                if active_tab_only:
                    cmd.append("--active-tab")
                if cv_track in {"ai_ml", "swe", "enterprise"}:
                    cmd.extend(["--cv-track", cv_track])

                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                log_path = repo_root / "data/autofill.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(log_path, "a", encoding="utf-8") as log_fh:
                        subprocess.Popen(
                            cmd,
                            cwd=str(repo_root),
                            stdin=subprocess.DEVNULL,
                            stdout=log_fh,
                            stderr=log_fh,
                            creationflags=creationflags,
                        )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "message": start_msg,
                            "cv": cv_info,
                        },
                    )
                except Exception as err:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Falha ao iniciar autofill: {err}"})
                return

            if path == "/api/apply-email":
                fp = str(data.get("fingerprint") or "").strip()
                to_email = str(data.get("to_email") or "").strip()
                subject = str(data.get("subject") or "").strip()
                body_txt = str(data.get("body") or "").strip()
                cv_track = str(data.get("cv_track") or "").strip() or None
                job = storage.get_job(fp)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Vaga não encontrada"})
                    return
                profile = load_candidate_profile(resolved_profile_path)
                kit = generate_application_kit(job, profile, override_track=cv_track, repo_root=repo_root, storage=storage)
                cv_path = Path(kit["cv"]["path"])
                ok, msg = send_application_email(
                    to_email=to_email,
                    subject=subject or kit["email_subject"],
                    body=body_txt or kit["email_body"],
                    attachment_path=cv_path,
                    profile=profile,
                )
                if ok:
                    today_str = datetime.now().strftime("%d/%m/%Y")
                    storage.update_job_status(
                        fp,
                        "applied",
                        f"Email enviado para {to_email} com {cv_path.name} em {today_str}",
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "message": msg, "marked_applied": True})
                else:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": msg, "marked_applied": False})
                return

            if path == "/api/scan":
                if scan_callback is None:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Scan indisponível"})
                    return
                if not scan_lock.acquire(blocking=False):
                    self._send_json(HTTPStatus.CONFLICT, {"error": "Já existe um scan em curso"})
                    return
                try:
                    summary = scan_callback()
                    jobs = [j.model_dump(mode="json") for j in storage.list_jobs(active_only=True)]
                    self._send_json(HTTPStatus.OK, {**summary, "jobs": jobs})
                except Exception as err:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(err)})
                finally:
                    scan_lock.release()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint não encontrado"})

    return RadarDashboardHandler


def serve_dashboard(
    storage: RadarStorage,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    scan_callback: Callable[[], dict[str, Any]] | None = None,
    profile_path: str | Path = DEFAULT_PROFILE_PATH,
) -> ThreadingHTTPServer:
    handler = make_handler(storage, scan_callback=scan_callback, profile_path=profile_path)
    server = _ReusableThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    logger.info("Estágio Radar PT Web UI ativo em %s", url)
    if sys.stdout is not None:
        print(f"Estágio Radar PT Web UI ativo em {url} (Ctrl+C para sair)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return server


def default_scan_runner(db_path: str, cache_path: str, output_dir: str) -> Callable[[], dict[str, Any]]:
    from estagio_radar.pipeline import run_radar_scan

    def _run() -> dict[str, Any]:
        result = asyncio.run(
            run_radar_scan(
                mode="fast",
                notify=False,
                db_path=db_path,
                cache_path=cache_path,
                output_dir=output_dir,
            )
        )
        return {
            "ok": True,
            "message": f"{len(result.active_jobs)} vagas ativas ({len(result.new_jobs)} novas)",
            "total": len(result.active_jobs),
            "new": len(result.new_jobs),
        }

    return _run
