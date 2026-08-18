import os
import json
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

logger = logging.getLogger("EmailNotifier")

def generate_html_email(jobs: list) -> str:
    now_str = datetime.now().strftime("%d/%m/%Y às %H:%M")
    total_jobs = len(jobs)

    # Agrupar por modalidade / localização
    lisbon_jobs = []
    remote_jobs = []
    other_jobs = []

    for j in jobs:
        loc = j.get("location", "").lower()
        title = j.get("title", "").lower()
        mod = j.get("modality", "").lower()

        if "lisbon" in loc or "lisboa" in loc or "oeiras" in loc or "albarraque" in loc:
            lisbon_jobs.append(j)
        elif "remote" in mod or "remoto" in mod or "100% remote" in loc:
            remote_jobs.append(j)
        else:
            other_jobs.append(j)

    def render_cards(job_list):
        if not job_list:
            return "<p style='color: #64748b; font-style: italic;'>Nenhuma nova vaga nesta categoria hoje.</p>"
        
        cards_html = ""
        for j in job_list:
            source = j.get("source", "Web")
            source_badge_color = {
                "LinkedIn": "#0a66c2",
                "Landing.jobs": "#00b289",
                "Himalayas": "#2563eb",
                "Jobicy": "#7c3aed",
                "Arbeitnow": "#ea580c"
            }.get(source, "#475569")

            # Teamlyzer Rating Badge
            score = j.get("company_score")
            reviews = j.get("company_reviews")
            teamlyzer_url = j.get("teamlyzer_url")

            if score and teamlyzer_url:
                rating_badge = f"""
                <a href="{teamlyzer_url}" target="_blank" style="background-color: #fef3c7; color: #92400e; font-size: 12px; font-weight: bold; padding: 3px 8px; border-radius: 6px; text-decoration: none; border: 1px solid #fde68a; display: inline-flex; align-items: center;">
                    {score} ({reviews}) ➔
                </a>
                """
            elif score:
                rating_badge = f"""
                <span style="background-color: #fef3c7; color: #92400e; font-size: 12px; font-weight: bold; padding: 3px 8px; border-radius: 6px;">
                    {score}
                </span>
                """
            else:
                rating_badge = """
                <span style="color: #94a3b8; font-size: 11px; font-style: italic;">
                    Sem rating PT
                </span>
                """

            cards_html += f"""
            <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <div>
                        <span style="background-color: {source_badge_color}; color: white; font-size: 11px; font-weight: bold; padding: 3px 8px; border-radius: 6px; text-transform: uppercase;">{source}</span>
                        <span style="background-color: #f1f5f9; color: #334155; font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; margin-left: 6px;">{j.get('modality', 'Híbrido/Remoto')}</span>
                    </div>
                    <span style="color: #94a3b8; font-size: 12px;">{j.get('post_date') or 'Recente'}</span>
                </div>
                <h3 style="margin: 8px 0 6px 0; font-size: 17px; color: #0f172a; font-weight: 700;">{j.get('title')}</h3>
                <div style="margin: 0 0 12px 0; color: #475569; font-size: 14px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <span><strong>Empresa:</strong> <span style="color: #0f172a; font-weight: 600;">{j.get('company')}</span></span>
                    <span>{rating_badge}</span>
                    <span>• <strong>Local:</strong> {j.get('location')}</span>
                </div>
                <div style="text-align: right; margin-top: 10px;">
                    <a href="{j.get('job_url')}" target="_blank" style="background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: bold; display: inline-block;">
                        Ver Vaga & Candidatar ➔
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
                <h1 style="margin: 0; font-size: 22px; font-weight: 800; letter-spacing: -0.5px;">🎯 Relatório Diário de Vagas de IA</h1>
                <p style="margin: 8px 0 0 0; color: #94a3b8; font-size: 14px;">Exclusivo: <strong>Junior | Trainee | Internship</strong> (com Scores Teamlyzer)</p>
                <div style="margin-top: 14px; display: inline-block; background: rgba(255,255,255,0.1); padding: 4px 14px; border-radius: 20px; font-size: 13px;">
                    ✨ <strong>{total_jobs} vagas qualificadas</strong> encontradas em {now_str}
                </div>
            </div>

            <div class="section-title">📍 Lisboa & Região (Híbrido / Presencial)</div>
            {render_cards(lisbon_jobs)}

            <div class="section-title">🌐 100% Remoto (Nacional & Internacional)</div>
            {render_cards(remote_jobs)}

            <div class="section-title">🇵🇹 Outras Localizações em Portugal & Traineeships</div>
            {render_cards(other_jobs)}

            <div class="footer">
                <p>Relatório gerado automaticamente pelo teu <strong>AI Job Aggregator</strong> no GitHub Actions.</p>
                <p>Scores e avaliações cruzados com o <strong>Teamlyzer Portugal</strong>.</p>
                <p>Ficheiro CSV completo anexado a este email.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def send_daily_email(json_path: str = "vagas_estritamente_junior_trainee_internship.json", csv_path: str = "vagas_estritamente_junior_trainee_internship.csv"):
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    receiver = os.getenv("RECEIVER_EMAIL") or "ruimiguelsa.stb@gmail.com"
    smtp_server = os.getenv("SMTP_SERVER") or "smtp.gmail.com"
    
    smtp_port_raw = os.getenv("SMTP_PORT")
    smtp_port = int(smtp_port_raw.strip()) if (smtp_port_raw and smtp_port_raw.strip().isdigit()) else 587

    if not (smtp_user and smtp_pass):
        print("Aviso: Env vars SMTP_USER/SMTP_PASS não configuradas. O envio de email foi ignorado.")
        return

    if not os.path.exists(json_path):
        print(f"Erro: Ficheiro {json_path} não encontrado.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    if not jobs:
        print("Aviso: Nenhuma vaga encontrada hoje para envio de email.")
        return

    subject = f"🎯 [Vagas IA] {len(jobs)} Novas Vagas Junior/Trainee (com Reviews) - {datetime.now().strftime('%d/%m/%Y')}"
    html_content = generate_html_email(jobs)

    msg = MIMEMultipart("mixed")
    msg["From"] = f"AI Job Finder <{smtp_user}>"
    msg["To"] = receiver
    msg["Subject"] = subject

    msg_body = MIMEMultipart("alternative")
    msg_body.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(msg_body)

    # Anexar CSV
    if os.path.exists(csv_path):
        try:
            with open(csv_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={os.path.basename(csv_path)}")
            msg.attach(part)
        except Exception as e:
            print(f"Aviso ao anexar CSV: {e}")

    try:
        print(f"A ligar a {smtp_server}:{smtp_port} para envio de email a {receiver}...")
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=20)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        print(f"✨ SUCESSO: Email enviado com sucesso para {receiver} com {len(jobs)} vagas e scores Teamlyzer!")
    except Exception as e:
        print(f"Erro ao enviar email via SMTP: {e}")

if __name__ == "__main__":
    send_daily_email()
