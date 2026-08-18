import json
import logging
import math
import os
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse


logger = logging.getLogger("EmailNotifier")


def generate_html_email(jobs: list) -> str:
    now_str = datetime.now().strftime("%d/%m/%Y às %H:%M")

    def rating(job: dict) -> float:
        try:
            value = float(job.get("rating_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    # A ordenacao por localizacao foi removida: quebrava a garantia global de
    # maior rating para menor rating no email.
    ordered_jobs = sorted(
        jobs,
        key=lambda job: (
            -rating(job),
            str(job.get("company", "")).casefold(),
            str(job.get("title", "")).casefold(),
        ),
    )

    def safe_text(value, fallback="") -> str:
        return escape(str(value if value is not None else fallback))

    def safe_url(value) -> str:
        value = str(value or "")
        try:
            parsed = urlparse(value)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return escape(value, quote=True)

    def render_cards(job_list: list) -> str:
        if not job_list:
            return "<p style='color: #64748b; font-style: italic;'>Nenhuma vaga qualificada hoje.</p>"

        cards_html = ""
        for job in job_list:
            raw_source = str(job.get("source", "Web"))
            source = safe_text(raw_source)
            source_badge_color = {
                "LinkedIn": "#0a66c2",
                "Landing.jobs": "#00b289",
                "Himalayas": "#2563eb",
                "Jobicy": "#7c3aed",
                "Arbeitnow": "#ea580c",
                "ITJobs.pt": "#0f766e",
            }.get(raw_source, "#475569")

            category = safe_text(job.get("category", "AI / ML"))
            if "ai" in category.lower():
                category_badge = (
                    '<span style="background-color: #ede9fe; color: #6d28d9; font-size: 11px; '
                    'font-weight: 700; padding: 3px 8px; border-radius: 6px;">IA &amp; ML</span>'
                )
            else:
                category_badge = (
                    '<span style="background-color: #e0f2fe; color: #0369a1; font-size: 11px; '
                    'font-weight: 700; padding: 3px 8px; border-radius: 6px;">TOP-TIER SWE</span>'
                )

            score = safe_text(job.get("company_score")) if job.get("company_score") else ""
            reviews = safe_text(job.get("company_reviews")) if job.get("company_reviews") else ""
            ranking_url = safe_url(job.get("teamlyzer_url"))
            job_url = safe_url(job.get("job_url"))

            if score and ranking_url:
                if "teamlyzer" in ranking_url.lower():
                    rating_badge = f"""
                    <a href="{ranking_url}" target="_blank" rel="noopener" style="background-color: #fef3c7; color: #92400e; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; text-decoration: none; border: 1px solid #fde68a; display: inline-flex; align-items: center; gap: 4px;">
                        <span>{score}</span> <span style="font-weight: 500; opacity: 0.85;">({reviews})</span> -&gt;
                    </a>
                    """
                else:
                    rating_badge = f"""
                    <a href="{ranking_url}" target="_blank" rel="noopener" style="background-color: #ecfdf5; color: #065f46; font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; text-decoration: none; border: 1px solid #a7f3d0; display: inline-flex; align-items: center; gap: 4px;">
                        <span>{score}</span> <span style="font-weight: 500; opacity: 0.85;">({reviews})</span> -&gt;
                    </a>
                    """
            elif score:
                rating_badge = f"""
                <span style="background-color: #f1f5f9; color: #334155; font-size: 12px; font-weight: 600; padding: 4px 8px; border-radius: 6px;">
                    {score}
                </span>
                """
            else:
                rating_badge = """
                <span style="color: #94a3b8; font-size: 11px; font-style: italic;">
                    Sem rating
                </span>
                """

            cards_html += f"""
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <div>
                        <span style="background-color: {source_badge_color}; color: white; font-size: 11px; font-weight: bold; padding: 3px 8px; border-radius: 6px; text-transform: uppercase;">{source}</span>
                        {category_badge}
                        <span style="background-color: #f1f5f9; color: #334155; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; margin-left: 4px;">{safe_text(job.get('modality', 'Híbrido/Remoto'))}</span>
                    </div>
                    <span style="color: #94a3b8; font-size: 12px;">{safe_text(job.get('post_date') or 'Recente')}</span>
                </div>
                <h3 style="margin: 8px 0 6px 0; font-size: 17px; color: #0f172a; font-weight: 700;">{safe_text(job.get('title'))}</h3>
                <div style="margin: 0 0 12px 0; color: #475569; font-size: 14px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <span><strong>Empresa:</strong> <span style="color: #0f172a; font-weight: 700; font-size: 15px;">{safe_text(job.get('company'))}</span></span>
                    <span>{rating_badge}</span>
                    <span>- <strong>Local:</strong> {safe_text(job.get('location'))}</span>
                </div>
                <div style="text-align: right; margin-top: 10px;">
                    <a href="{job_url}" target="_blank" rel="noopener" style="background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: bold; display: inline-block;">
                        Ver Vaga &amp; Candidatar -&gt;
                    </a>
                </div>
            </div>
            """
        return cards_html

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 680px; margin: 0 auto; background: #f8fafc; }}
            .header {{ background: linear-gradient(135deg, #1e293b, #0f172a); color: white; padding: 26px 20px; border-radius: 12px; text-align: center; margin-bottom: 24px; }}
            .section-title {{ font-size: 18px; color: #1e293b; font-weight: 800; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; margin: 24px 0 14px 0; }}
            .footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 30px; padding: 15px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.5px;">Vagas Junior, Trainee &amp; Internships</h1>
                <p style="margin: 8px 0 0 0; color: #94a3b8; font-size: 14px;">IA/ML + Top-Tier Software Engineering (Ordenadas por Rating da Empresa)</p>
                <div style="margin-top: 14px; display: inline-block; background: rgba(255,255,255,0.1); padding: 4px 14px; border-radius: 20px; font-size: 13px;">
                    <strong>{len(ordered_jobs)} vagas qualificadas</strong> ordenadas pelo maior score (Teamlyzer / Glassdoor)
                </div>
            </div>

            <div class="section-title">Vagas qualificadas por rating</div>
            {render_cards(ordered_jobs)}

            <div class="footer">
                <p>Relatorio gerado em {safe_text(now_str)} pelo teu <strong>AI &amp; Top-Tech Job Aggregator</strong>.</p>
                <p>Ordenacao decrescente: vagas de empresas com melhor rating aparecem sempre no topo.</p>
                <p>Ficheiro CSV completo anexado a este email.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_daily_email(
    json_path: str = "vagas_estritamente_junior_trainee_internship.json",
    csv_path: str = "vagas_estritamente_junior_trainee_internship.csv",
):
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    receiver = os.getenv("RECEIVER_EMAIL")
    smtp_server = os.getenv("SMTP_SERVER") or "smtp.gmail.com"

    smtp_port_raw = os.getenv("SMTP_PORT")
    smtp_port = int(smtp_port_raw.strip()) if smtp_port_raw and smtp_port_raw.strip().isdigit() else 587

    if not (smtp_user and smtp_pass and receiver):
        print("Aviso: SMTP_USER, SMTP_PASS e RECEIVER_EMAIL têm de estar configuradas. O envio foi ignorado.")
        return

    if not os.path.exists(json_path):
        print(f"Erro: Ficheiro {json_path} não encontrado.")
        return

    with open(json_path, "r", encoding="utf-8") as file:
        jobs = json.load(file)

    if not jobs:
        print("Aviso: Nenhuma vaga encontrada hoje para envio de email.")
        return

    subject = f"[Vagas Top Tech] {len(jobs)} Vagas Junior/Internship Ordenadas por Rating - {datetime.now().strftime('%d/%m/%Y')}"
    html_content = generate_html_email(jobs)

    message = MIMEMultipart("mixed")
    message["From"] = f"Top-Tech Job Finder <{smtp_user}>"
    message["To"] = receiver
    message["Subject"] = subject

    message_body = MIMEMultipart("alternative")
    message_body.attach(MIMEText(html_content, "html", "utf-8"))
    message.attach(message_body)

    if os.path.exists(csv_path):
        try:
            with open(csv_path, "rb") as file:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(file.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(csv_path)}")
            message.attach(part)
        except OSError as error:
            print(f"Aviso ao anexar CSV: {error}")

    try:
        print(f"A ligar a {smtp_server}:{smtp_port} para envio de email a {receiver}...")
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(message)
        print(f"SUCESSO: Email enviado para {receiver} com {len(jobs)} vagas ordenadas por rating!")
    except Exception as error:
        print(f"Erro ao enviar email via SMTP: {error}")


if __name__ == "__main__":
    send_daily_email()
