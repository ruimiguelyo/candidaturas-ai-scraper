"""Servidor web interativo local para consultar, filtrar e lançar candidaturas 1-clique no browser."""

from __future__ import annotations

import asyncio
import json
import logging
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

from estagio_radar.apply_copilot import (
    DEFAULT_PROFILE_PATH,
    generate_application_kit,
    list_available_cvs,
    load_candidate_profile,
    send_application_email,
)
from estagio_radar.exporters import render_dashboard_html
from estagio_radar.storage import RadarStorage

logger = logging.getLogger("estagio_radar.dashboard")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
                        "full_name": profile.get("full_name", ""),
                        "email": profile.get("email", ""),
                        "phone": profile.get("phone", ""),
                        "location": profile.get("location", ""),
                        "university": profile.get("university", ""),
                        "degree": profile.get("degree", ""),
                        "gpa": profile.get("gpa", ""),
                        "linkedin_url": profile.get("linkedin_url", ""),
                        "github_url": profile.get("github_url", ""),
                        "cvs": list_available_cvs(profile, repo_root=repo_root),
                    },
                )
                return
            if path == "/api/apply-kit":
                fp = (qs.get("fingerprint") or [""])[0].strip()
                cv_track = (qs.get("cv_track") or [""])[0].strip() or None
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
                )
                self._send_json(HTTPStatus.OK, kit)
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
                try:
                    updated = storage.update_job_status(
                        fp, status, str(notes) if notes is not None else None
                    )
                except ValueError as err:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": updated})
                return

            if path == "/api/autofill":
                fp = str(data.get("fingerprint") or "").strip()
                cv_track = str(data.get("cv_track") or "").strip() or None
                job = storage.get_job(fp)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Vaga não encontrada"})
                    return
                profile = load_candidate_profile(resolved_profile_path)
                kit = generate_application_kit(job, profile, override_track=cv_track, repo_root=repo_root)

                py_exe = Path(sys.executable)
                if py_exe.name.lower() == "pythonw.exe":
                    alt = py_exe.with_name("python.exe")
                    if alt.exists():
                        py_exe = alt

                cmd = [
                    str(py_exe),
                    "-m",
                    "estagio_radar",
                    "autofill",
                    "--fingerprint",
                    fp,
                    "--db",
                    str(storage.db_path),
                    "--profile",
                    str(resolved_profile_path),
                ]
                if cv_track in {"ai_ml", "swe", "enterprise"}:
                    cmd.extend(["--cv-track", cv_track])

                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
                try:
                    subprocess.Popen(
                        cmd,
                        cwd=str(repo_root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "message": f"Chrome a abrir com auto-preenchimento e CV {kit['cv']['filename']}...",
                            "cv": kit["cv"],
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
                kit = generate_application_kit(job, profile, override_track=cv_track, repo_root=repo_root)
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
