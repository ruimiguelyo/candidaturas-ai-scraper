"""Interface de linha de comandos (CLI) do Estágio Radar PT v4.0."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from estagio_radar.apply_copilot import DEFAULT_PROFILE_PATH, generate_application_kit, load_candidate_profile
from estagio_radar.autofill_worker import run_autofill_browser
from estagio_radar.dashboard import default_scan_runner, serve_dashboard
from estagio_radar.notifiers.ntfy_notifier import send_ntfy_message
from estagio_radar.pipeline import run_radar_scan
from estagio_radar.scrapers.base import ETagCache
from estagio_radar.storage import RadarStorage

_LOG_STREAM: TextIO | None = None


def _configure_stdio(log_file: str | None = None) -> None:
    """Configura stdout/stderr para TTY, pythonw.exe (streams None) ou ficheiro com rotação."""
    global _LOG_STREAM
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            backup = Path(f"{path}.1")
            try:
                path.replace(backup)
            except OSError:
                pass
        if _LOG_STREAM is not None and not _LOG_STREAM.closed:
            try:
                _LOG_STREAM.close()
            except OSError:
                pass
        stream = open(path, "a", encoding="utf-8", buffering=1)
        _LOG_STREAM = stream
        sys.stdout = stream
        sys.stderr = stream
        return

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    elif sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    elif sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _build_console() -> Console:
    is_tty = bool(sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)())
    if is_tty:
        return Console(force_terminal=True, legacy_windows=False)
    return Console(file=sys.stdout, force_terminal=False, no_color=True, width=140)


def _acquire_single_instance_lock(lock_path: Path, pid_path: Path) -> Any | None:
    """Adquire um lock exclusivo não-bloqueante para garantir apenas 1 daemon watch ativo."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        f.seek(0)
        if not f.read(1):
            f.write("0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None

    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return f


def _release_single_instance_lock(lock_handle: Any, pid_path: Path | None = None) -> None:
    if lock_handle is None:
        return
    try:
        lock_handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            lock_handle.close()
        except OSError:
            pass
    if pid_path is not None and pid_path.exists():
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_pid(pid_path: Path) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


def _is_daemon_running(lock_path: Path, pid_path: Path) -> tuple[bool, int | None]:
    """Verifica se o daemon watch está ativo tentando adquirir o lock sem alterar o PID."""
    if not lock_path.exists():
        return False, None
    try:
        f = open(lock_path, "r+", encoding="utf-8")
    except OSError:
        return True, _read_pid(pid_path)

    try:
        f.seek(0)
        if not f.read(1):
            f.close()
            return False, None
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
        return False, None
    except OSError:
        f.close()
        return True, _read_pid(pid_path)


def _is_port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="estagio-radar",
        description="Estágio Radar PT v4.0 — Modo Sniper ao Minuto para Setúbal, Lisboa e Remoto PT.",
    )
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = sub.add_parser("scan", help="Executa uma recolha (sniper, fast ou full).")
    p_scan.add_argument("--mode", choices=("sniper", "fast", "full"), default="full")
    p_scan.add_argument("--notify", action="store_true", help="Envia alertas apenas para vagas NOVAS.")
    p_scan.add_argument("--notify-all", action="store_true", help="Envia alertas com todas as vagas ativas.")
    p_scan.add_argument("--hide-consultancies", action="store_true", help="Oculta consultoras/outsourcing.")
    p_scan.add_argument("--all-portugal", action="store_true", help="Inclui também Braga/Aveiro/Porto presenciais.")
    p_scan.add_argument("--min-score", type=float, default=0.0)
    p_scan.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))
    p_scan.add_argument("--cache", default=os.getenv("COMPANY_CACHE_PATH", "data/company_cache.json"))
    p_scan.add_argument("--output-dir", default=os.getenv("RADAR_OUTPUT_DIR", "output"))

    # watch (2 velocidades: Sniper ao minuto + Deep sweep horário + dashboard opcional)
    p_watch = sub.add_parser("watch", help="Modo contínuo Sniper ao minuto (com cache ETag 304).")
    p_watch.add_argument("--sniper-interval", type=int, default=5, help="Intervalo Sniper em minutos (predefinição: 5).")
    p_watch.add_argument("--deep-interval", type=int, default=60, help="Intervalo Deep sweep em minutos (predefinição: 60).")
    p_watch.add_argument("--notify", action="store_true", help="Envia alertas instantâneos para vagas NOVAS.")
    p_watch.add_argument("--hide-consultancies", action="store_true")
    p_watch.add_argument("--all-portugal", action="store_true")
    p_watch.add_argument("--with-ui", action="store_true", help="Mantém também o dashboard web local ativo em segundo plano.")
    p_watch.add_argument("--ui-port", type=int, default=8765, help="Porta do dashboard web local (predefinição: 8765).")
    p_watch.add_argument("--log-file", default=None, help="Ficheiro de log para execução silenciosa (ex: data/radar.log).")
    p_watch.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))
    p_watch.add_argument("--cache", default=os.getenv("COMPANY_CACHE_PATH", "data/company_cache.json"))
    p_watch.add_argument("--output-dir", default=os.getenv("RADAR_OUTPUT_DIR", "output"))

    # ui
    p_ui = sub.add_parser("ui", help="Inicia o dashboard interativo local no browser.")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true")
    p_ui.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))
    p_ui.add_argument("--cache", default=os.getenv("COMPANY_CACHE_PATH", "data/company_cache.json"))
    p_ui.add_argument("--output-dir", default=os.getenv("RADAR_OUTPUT_DIR", "output"))

    # status
    p_stat = sub.add_parser("status", help="Mostra resumo da base de dados SQLite, daemon e último scan.")
    p_stat.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))
    p_stat.add_argument("--ui-port", type=int, default=8765)

    # mark-notified
    p_mark = sub.add_parser("mark-notified", help="Marca todas as vagas ativas atuais como já notificadas.")
    p_mark.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))

    # notify-test
    p_ntest = sub.add_parser("notify-test", help="Envia uma notificação de teste via ntfy.sh.")
    p_ntest.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))

    # autofill
    p_auto = sub.add_parser("autofill", help="Abre uma vaga no Chrome e pré-preenche dados + CV PDF sem submeter.")
    p_auto.add_argument("--fingerprint", required=True)
    p_auto.add_argument("--cv-track", choices=("ai_ml", "swe", "enterprise"), default=None)
    p_auto.add_argument("--db", default=os.getenv("RADAR_DB_PATH", "data/radar.db"))
    p_auto.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH))
    p_auto.add_argument("--no-keep-open", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    load_dotenv(Path.cwd() / ".env")
    args = build_parser().parse_args(argv)
    if getattr(args, "log_file", None):
        _configure_stdio(args.log_file)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    console = _build_console()

    if args.command == "scan":
        asyncio.run(
            run_radar_scan(
                mode=args.mode,
                notify=args.notify,
                notify_all=args.notify_all,
                hide_consultancies=args.hide_consultancies,
                include_all_portugal=args.all_portugal,
                min_score=args.min_score,
                db_path=args.db,
                cache_path=args.cache,
                output_dir=args.output_dir,
                console=console,
            )
        )
        return 0

    if args.command == "watch":
        db_parent = Path(args.db).parent
        lock_path = db_parent / "radar_watch.lock"
        pid_path = db_parent / "radar_watch.pid"
        lock_handle = _acquire_single_instance_lock(lock_path, pid_path)
        if lock_handle is None:
            console.print("Estágio Radar watch já está em execução — a terminar duplicado.")
            return 0

        ui_server = None
        try:
            if args.with_ui:
                try:
                    ui_storage = RadarStorage(args.db)
                    ui_server = serve_dashboard(
                        ui_storage,
                        port=args.ui_port,
                        open_browser=False,
                        scan_callback=default_scan_runner(args.db, args.cache, args.output_dir),
                    )
                    ui_thread = threading.Thread(target=ui_server.serve_forever, daemon=True)
                    ui_thread.start()
                    console.print(f"[green]Dashboard Web UI ativo em http://127.0.0.1:{args.ui_port}[/green]")
                except OSError as err:
                    console.print(f"[yellow]Aviso: Web UI não iniciado na porta {args.ui_port}: {err}[/yellow]")

            sniper_sec = max(60, args.sniper_interval * 60)
            deep_sec = max(sniper_sec, args.deep_interval * 60)
            console.print(
                f"[bold cyan]Modo Sniper ao Minuto ativo (PID {os.getpid()}):[/bold cyan] "
                f"ATS + LinkedIn 1h a cada {args.sniper_interval} min | "
                f"Deep sweep (Trackers EU + EURAXESS + Net-Empregos) a cada {args.deep_interval} min."
            )
            if hasattr(sys.stdout, "flush"):
                sys.stdout.flush()
            etag_cache = ETagCache()

            async def _loop() -> None:
                last_deep_ts = 0.0
                while True:
                    now_mono = time.monotonic()
                    run_mode = "full" if (last_deep_ts == 0.0 or now_mono - last_deep_ts >= deep_sec) else "sniper"
                    ts_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    console.print(f"[{ts_start}] A iniciar ciclo {run_mode.upper()}...")
                    try:
                        await run_radar_scan(
                            mode=run_mode,
                            notify=args.notify,
                            hide_consultancies=args.hide_consultancies,
                            include_all_portugal=args.all_portugal,
                            db_path=args.db,
                            cache_path=args.cache,
                            output_dir=args.output_dir,
                            console=console,
                            etag_cache=etag_cache,
                        )
                        if run_mode == "full":
                            last_deep_ts = time.monotonic()
                    except Exception as err:
                        console.print(f"[red]Erro no ciclo {run_mode}:[/red] {err}")
                    ts_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    console.print(
                        f"[{ts_end}] Ciclo {run_mode.upper()} concluído. Próximo sniper em {args.sniper_interval} min."
                    )
                    if hasattr(sys.stdout, "flush"):
                        sys.stdout.flush()
                    await asyncio.sleep(sniper_sec)

            try:
                asyncio.run(_loop())
            except KeyboardInterrupt:
                console.print("[yellow]Watch terminado pelo utilizador.[/yellow]")
            return 0
        finally:
            if ui_server is not None:
                try:
                    ui_server.shutdown()
                    ui_server.server_close()
                except OSError:
                    pass
            _release_single_instance_lock(lock_handle, pid_path)

    if args.command == "ui":
        storage = RadarStorage(args.db)
        srv = serve_dashboard(
            storage,
            port=args.port,
            open_browser=not args.no_browser,
            scan_callback=default_scan_runner(args.db, args.cache, args.output_dir),
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
        return 0

    if args.command == "mark-notified":
        storage = RadarStorage(args.db)
        marked = storage.mark_all_active_notified()
        console.print(f"[green]{marked} vaga(s) ativa(s) marcada(s) como notificadas (baseline pronta).[/green]")
        return 0

    if args.command == "notify-test":
        storage = RadarStorage(args.db)
        stats = storage.get_stats()
        active_cnt = stats["total_active"]
        ok = asyncio.run(
            send_ntfy_message(
                title="Estagio Radar PT v4.0 Ativo!",
                body=(
                    f"Sniper ao minuto pronto para Setubal, Lisboa & Remoto PT.\n"
                    f"Vagas ativas no radar: {active_cnt} (sem spam — so avisamos NOVAS!).\n"
                    f"Dashboard local: http://127.0.0.1:8765"
                ),
                priority="3",
                tags="rocket,white_check_mark",
            )
        )
        if ok:
            console.print("[bold green]Notificação de teste enviada via ntfy.sh com sucesso![/bold green]")
            return 0
        console.print("[bold red]Falha ao enviar notificação ntfy. Verifica NTFY_TOPIC no .env.[/bold red]")
        return 1

    if args.command == "autofill":
        storage = RadarStorage(args.db)
        job = storage.get_job(args.fingerprint)
        if job is None:
            console.print(f"[bold red]Vaga não encontrada:[/bold red] {args.fingerprint}")
            return 1
        profile = load_candidate_profile(args.profile)
        kit = generate_application_kit(job, profile, override_track=args.cv_track)
        res = run_autofill_browser(
            job,
            kit,
            profile,
            headless=False,
            keep_open=not args.no_keep_open,
        )
        console.print(f"[green]Autofill concluído:[/green] {res}")
        return 0

    if args.command == "status":
        db_parent = Path(args.db).parent
        running, daemon_pid = _is_daemon_running(
            db_parent / "radar_watch.lock",
            db_parent / "radar_watch.pid",
        )
        ui_up = _is_port_listening("127.0.0.1", args.ui_port)
        storage = RadarStorage(args.db)
        stats = storage.get_stats()
        t = Table(title="ESTADO DO ESTÁGIO RADAR PT")
        t.add_column("Métrica")
        t.add_column("Valor", justify="right")
        daemon_txt = f"Ativo (PID {daemon_pid})" if running and daemon_pid else ("Ativo" if running else "Parado")
        t.add_row("Daemon Sniper", daemon_txt)
        t.add_row("Dashboard Local", f"Ativo (http://127.0.0.1:{args.ui_port})" if ui_up else "Inativo")
        t.add_row("Vagas ativas", str(stats["total_active"]))
        t.add_row("Por estado", str(stats["by_status"]))
        t.add_row("Por zona", str(stats["by_zone"]))
        t.add_row("Por tipo", str(stats["by_tier"]))
        if stats["last_run"]:
            lr = stats["last_run"]
            t.add_row("Último scan", f"{lr['finished_at']} ({lr['mode']}, {lr['accepted_count']} aceites, {lr['new_count']} novas)")
        console.print(t)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
