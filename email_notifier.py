import json
import logging
import math
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse


logger = logging.getLogger("EmailNotifier")


def _rating(job: dict) -> float:
    try:
        value = float(job.get("rating_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _safe_text(value, fallback="") -> str:
    return escape(str(value if value is not None else fallback))


def _safe_url(value) -> str:
    value = str(value or "")
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(value, quote=True)


def _location_section(job: dict) -> str:
    location = str(job.get("location", "")).casefold()
    modality = str(job.get("modality", "")).casefold()
    if any(place in location for place in ("lisbon", "lisboa", "oeiras", "albarraque")):
        return "lisbon"
    if "remote" in modality or "remoto" in modality or "100% remote" in location:
        return "remote"
    return "other"


def _render_outreach(job: dict) -> str:
    outreach = job.get("human_outreach")
    if not isinstance(outreach, dict) or not outreach.get("target_found"):
        return ""
    profile_url = _safe_url(outreach.get("profile_url"))
    profile_link = f' <a href="{profile_url}">perfil</a>' if profile_url else ""
    evidence = outreach.get("evidence") or []
    evidence_text = evidence[0] if isinstance(evidence, list) and evidence else ""
    return (
        '<aside><b>UNVERIFIED CONTACT:</b> '
        f'{_safe_text(outreach.get("name") or "Contacto")} — '
        f'{_safe_text(outreach.get("current_title") or "função por confirmar")}{profile_link}<br>'
        f'<b>Search confidence: {_safe_text(outreach.get("confidence") or "MEDIUM")}</b>. '
        'Please verify the person and role before use. '
        f'{_safe_text(evidence_text)}'
        f'<br><i>{_safe_text(outreach.get("suggested_message") or "")}</i></aside>'
    )


def _render_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return '<p class="empty">Nenhuma vaga nesta secção.</p>'
    rows = []
    for job in jobs:
        url = _safe_url(job.get("job_url"))
        title = _safe_text(job.get("title") or "Vaga sem título")
        title_html = f'<a href="{url}"><b>{title}</b></a>' if url else f"<b>{title}</b>"
        rating = _safe_text(job.get("company_score") or "sem rating")
        compatibility = _safe_text(job.get("location_compatibility") or "unknown")
        rows.append(
            "<li>"
            f"{title_html}"
            "<small>"
            f'{_safe_text(job.get("company") or "Empresa desconhecida")} · '
            f'{_safe_text(job.get("location") or "Local desconhecido")} · '
            f'{_safe_text(job.get("post_date") or "data desconhecida")} · '
            f'{_safe_text(job.get("source") or "Web")} · {rating} · {compatibility}'
            "</small>"
            f"{_render_outreach(job)}"
            "</li>"
        )
    return "<ol>" + "".join(rows) + "</ol>"


def generate_html_email(jobs: list, count_label: str = "vagas qualificadas") -> str:
    """Render one compact report that remains below Gmail's clipping threshold."""
    ordered_jobs = sorted(
        jobs,
        key=lambda job: (
            -_rating(job),
            str(job.get("company", "")).casefold(),
            str(job.get("title", "")).casefold(),
        ),
    )
    sections = {"lisbon": [], "remote": [], "other": []}
    for job in ordered_jobs:
        sections[_location_section(job)].append(job)

    now = _safe_text(datetime.now().strftime("%d/%m/%Y às %H:%M"))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{font:14px Arial,sans-serif;color:#172033;background:#f5f7fb;margin:0;padding:14px}}
main{{max-width:760px;margin:auto;background:white;padding:18px;border-radius:10px}}
h1{{font-size:22px;margin:0 0 5px}}h2{{font-size:17px;margin:22px 0 6px;border-bottom:2px solid #dbe4f0;padding-bottom:5px}}
.summary{{background:#eaf2ff;padding:10px;border-radius:7px}}ol{{margin:0;padding-left:25px}}li{{padding:7px 3px;border-bottom:1px solid #edf0f5}}
a{{color:#075fc9;text-decoration:none}}small{{display:block;color:#58657a;margin-top:2px}}aside{{font-size:12px;background:#f4f6f8;padding:6px;margin-top:5px}}
.empty{{color:#748095}}footer{{font-size:11px;color:#748095;margin-top:20px}}
</style></head><body><main>
<h1>Vagas Junior, Trainee &amp; Internships</h1>
<p class="summary"><b>{len(ordered_jobs)} {_safe_text(count_label)}</b> · apenas publicações verificadas dos últimos 7 dias · relatório completo num único email.</p>
<h2>📍 Lisboa &amp; Região</h2>{_render_jobs(sections['lisbon'])}
<h2>🌐 100% Remoto</h2>{_render_jobs(sections['remote'])}
<h2>🇵🇹 Outras Localizações</h2>{_render_jobs(sections['other'])}
<footer>Gerado em {now}. Ordenação por rating dentro de cada secção. Sem anexo CSV.</footer>
</main></body></html>"""


def send_daily_email(
    json_path: str = "vagas_estritamente_junior_trainee_internship.json",
    csv_path: str = "vagas_estritamente_junior_trainee_internship.csv",
    jobs: list | None = None,
    new_only: bool = False,
) -> bool:
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    receiver = os.getenv("RECEIVER_EMAIL")
    smtp_server = os.getenv("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port_raw = os.getenv("SMTP_PORT")
    smtp_port = int(smtp_port_raw.strip()) if smtp_port_raw and smtp_port_raw.strip().isdigit() else 587
    _ = csv_path  # Retained for backwards-compatible callers; CSV is intentionally not attached.

    if not (smtp_user and smtp_pass and receiver):
        logger.error("SMTP_USER, SMTP_PASS e RECEIVER_EMAIL têm de estar configuradas.")
        return False
    if jobs is None:
        if not os.path.exists(json_path):
            logger.error("Ficheiro %s não encontrado.", json_path)
            return False
        try:
            with open(json_path, "r", encoding="utf-8") as file:
                jobs = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Não foi possível carregar %s: %s", json_path, error)
            return False

    scope_label = "Novas vagas" if new_only else "Vagas dos últimos 7 dias"
    count_label = "novas vagas qualificadas" if new_only else "vagas qualificadas"
    subject = (
        f"[Vagas Top Tech] {len(jobs)} {scope_label} Junior/Internship - "
        f"{datetime.now().strftime('%d/%m/%Y')}"
    )
    message = MIMEMultipart("alternative")
    message["From"] = f"Top-Tech Job Finder <{smtp_user}>"
    message["To"] = receiver
    message["Subject"] = subject
    message.attach(MIMEText(generate_html_email(jobs, count_label=count_label), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(message)
        logger.info("Um email enviado para %s com %s vagas.", receiver, len(jobs))
        return True
    except Exception as error:
        logger.error("Erro ao enviar email via SMTP: %s", error)
        return False


if __name__ == "__main__":
    send_daily_email()
