"""Digest HTML limpo via SMTP agrupado por proximidade a Setúbal, Estágio vs Junior e Produto vs Consultora."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse

from estagio_radar.models import EnrichedJob, GeoZone, RoleTier

logger = logging.getLogger("estagio_radar.notifiers.email")


def _safe_url(value: str | None) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(raw, quote=True)


def _score_color(score: float) -> str:
    if score >= 82:
        return "#059669"
    if score >= 68:
        return "#2563eb"
    if score >= 52:
        return "#d97706"
    return "#64748b"


def _render_job_row(job: EnrichedJob) -> str:
    cp = job.company_profile
    url = _safe_url(job.job_url)
    tl_url = _safe_url(cp.teamlyzer_url)
    title_html = f'<a href="{url}" style="color:#0f172a;font-weight:700;text-decoration:none">{escape(job.title)}</a>' if url else escape(job.title)

    tl_part = (
        f'★ {cp.teamlyzer_rating:.1f}/5 ({cp.teamlyzer_reviews} rev.)'
        if cp.teamlyzer_rating
        else "Sem rating Teamlyzer"
    )
    if cp.avg_salary_eur:
        tl_part += f" · €{cp.avg_salary_eur // 1000}k méd."
    if tl_url:
        tl_part = f'<a href="{tl_url}" style="color:#475569;text-decoration:underline">{escape(tl_part)}</a>'
    else:
        tl_part = escape(tl_part)

    badges_html = " ".join(
        f'<span style="display:inline-block;background:#f1f5f9;color:#334155;font-size:11px;padding:2px 6px;border-radius:4px;margin-right:3px">{escape(b)}</span>'
        for b in job.badges[:5]
    )
    apply_btn = (
        f'<a href="{url}" style="display:inline-block;background:#2563eb;color:#ffffff;font-size:12px;font-weight:600;padding:5px 10px;border-radius:6px;text-decoration:none;white-space:nowrap">Candidatar →</a>'
        if url
        else ""
    )

    return (
        '<li style="padding:10px 4px;border-bottom:1px solid #e2e8f0;list-style:none">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top">'
        f'<span style="display:inline-block;background:{_score_color(job.score)};color:#fff;font-weight:700;font-size:12px;padding:2px 7px;border-radius:999px;margin-right:6px">{job.score:.0f}</span>'
        f"{title_html}"
        '<div style="color:#475569;font-size:13px;margin-top:4px">'
        f'<b>{escape(job.company)}</b> · {escape(job.location)} ({escape(job.modality)}) · '
        f'{escape(job.freshness_label)} · {escape(", ".join(job.sources))} · {tl_part}'
        "</div>"
        f'<div style="margin-top:5px">{badges_html}</div>'
        "</td>"
        f'<td style="vertical-align:middle;text-align:right;padding-left:10px">{apply_btn}</td>'
        "</tr></table>"
        "</li>"
    )


def _render_section(title: str, jobs: list[EnrichedJob]) -> str:
    if not jobs:
        return ""
    rows = "".join(_render_job_row(j) for j in jobs)
    return (
        f'<h2 style="font-size:16px;color:#0f172a;margin:22px 0 8px;padding-bottom:6px;border-bottom:2px solid #cbd5e1">'
        f"{escape(title)} ({len(jobs)})</h2>"
        f'<ul style="margin:0;padding:0">{rows}</ul>'
    )


def generate_email_html(jobs: list[EnrichedJob], *, new_only: bool = True) -> str:
    ordered = sorted(jobs, key=lambda j: (-j.score, j.company.casefold(), j.title.casefold()))

    consultancies = [j for j in ordered if j.company_profile.is_consultancy]
    core = [j for j in ordered if not j.company_profile.is_consultancy]

    setubal_interns = [j for j in core if j.geo_zone == GeoZone.SETUBAL_LOCAL and j.role_tier == RoleTier.INTERNSHIP]
    lisboa_interns = [j for j in core if j.geo_zone == GeoZone.LISBOA_METRO and j.role_tier == RoleTier.INTERNSHIP]
    remote_interns = [
        j for j in core
        if j.geo_zone in {GeoZone.REMOTE_PT, GeoZone.PT_UNSPECIFIED, GeoZone.PT_OTHER_CITY}
        and j.role_tier == RoleTier.INTERNSHIP
    ]
    entry_juniors = [j for j in core if j.role_tier == RoleTier.ENTRY_JUNIOR]

    mode_lbl = "novas oportunidades detetadas agora" if new_only else "oportunidades ativas qualificadas"
    now_str = datetime.now().strftime("%d/%m/%Y às %H:%M")

    sections_html = (
        _render_section("📍 Estágios em Setúbal, Palmela & Margem Sul", setubal_interns)
        + _render_section("🚆 Estágios em Lisboa, Oeiras & Grande Lisboa", lisboa_interns)
        + _render_section("🌐 Estágios Remotos (Elegível PT) & Portugal", remote_interns)
        + _render_section("🚀 Entry-Level / Junior (0–1 ano, Empresas de Produto)", entry_juniors)
        + _render_section("⚠️ Consultoras & Outsourcing", consultancies)
    )

    return f"""<!doctype html>
<html lang="pt-PT"><head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;color:#0f172a;margin:0;padding:16px">
<div style="max-width:780px;margin:0 auto;background:#ffffff;border-radius:12px;padding:22px;border:1px solid #e2e8f0">
  <h1 style="font-size:21px;margin:0 0 6px;color:#0f172a">🎯 Estágio Radar PT — Informática &amp; IA</h1>
  <p style="margin:0 0 14px;background:#eff6ff;color:#1e3a8a;padding:10px 12px;border-radius:8px;font-size:14px">
    <b>{len(ordered)} {escape(mode_lbl)}</b> · ordenado por Score 0–100 (Estágio + Empresa Produto + Setúbal/Lisboa + Frescura).
  </p>
  {sections_html or '<p style="color:#64748b">Sem oportunidades nesta seleção.</p>'}
  <p style="margin:20px 0 0;font-size:11px;color:#64748b">
    Gerado em {escape(now_str)} por Estágio Radar PT v4.0 · Foco: Setúbal, Palmela, Margem Sul, Lisboa &amp; Remoto PT.
  </p>
</div>
</body></html>"""


def send_email_alert(jobs: list[EnrichedJob], *, new_only: bool = True) -> bool:
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    receiver = (os.getenv("RECEIVER_EMAIL") or "").strip()
    smtp_server = (os.getenv("SMTP_SERVER") or "smtp.gmail.com").strip()
    port_raw = (os.getenv("SMTP_PORT") or "587").strip()
    smtp_port = int(port_raw) if port_raw.isdigit() else 587

    if not (smtp_user and smtp_pass and receiver) or not jobs:
        return False

    top_company = jobs[0].company
    prefix = "NOVO" if new_only else "Resumo"
    subject = (
        f"[Estágio Radar] {len(jobs)} {prefix}: {top_company} — "
        f"{jobs[0].title[:45]} ({datetime.now().strftime('%d/%m')})"
    )

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Estágio Radar PT <{smtp_user}>"
    msg["To"] = receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(generate_email_html(jobs, new_only=new_only), "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info("Email enviado para %s com %s vaga(s).", receiver, len(jobs))
        return True
    except Exception as err:
        logger.warning("Erro ao enviar email SMTP: %s", err)
        return False
