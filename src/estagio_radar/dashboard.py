"""Servidor web interativo local para consultar, filtrar e gerir candidaturas no browser."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from estagio_radar.exporters import render_dashboard_html
from estagio_radar.storage import RadarStorage

logger = logging.getLogger("estagio_radar.dashboard")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def make_handler(
    storage: RadarStorage,
    scan_callback: Callable[[], dict[str, Any]] | None = None,
):
    scan_lock = threading.Lock()

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
            path = self.path.split("?", 1)[0]
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
) -> ThreadingHTTPServer:
    handler = make_handler(storage, scan_callback=scan_callback)
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
