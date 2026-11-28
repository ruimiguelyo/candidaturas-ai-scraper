"""Exportadores públicos (CSV seguro, JSON, Auditoria de Rejeições e Dashboard HTML)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from estagio_radar.models import EnrichedJob

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    stripped = text.lstrip()
    if stripped and stripped[0] in CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def export_csv(jobs: list[EnrichedJob], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "score",
        "title",
        "company",
        "company_tier",
        "company_type",
        "is_consultancy",
        "teamlyzer_rating",
        "teamlyzer_reviews",
        "avg_salary_eur",
        "role_tier",
        "domain",
        "geo_zone",
        "location",
        "modality",
        "freshness",
        "published_date",
        "sources",
        "salary",
        "badges",
        "user_status",
        "job_url",
        "teamlyzer_url",
        "fingerprint",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for j in jobs:
            cp = j.company_profile
            writer.writerow({
                "score": j.score,
                "title": safe_csv_cell(j.title),
                "company": safe_csv_cell(j.company),
                "company_tier": cp.company_tier,
                "company_type": cp.company_type.value,
                "is_consultancy": cp.is_consultancy,
                "teamlyzer_rating": cp.teamlyzer_rating or "",
                "teamlyzer_reviews": cp.teamlyzer_reviews or 0,
                "avg_salary_eur": cp.avg_salary_eur or "",
                "role_tier": j.role_tier.value,
                "domain": j.domain.value,
                "geo_zone": j.geo_zone.value,
                "location": safe_csv_cell(j.location),
                "modality": j.modality,
                "freshness": j.freshness_label,
                "published_date": j.published_date,
                "sources": safe_csv_cell(", ".join(j.sources)),
                "salary": safe_csv_cell(j.salary or ""),
                "badges": safe_csv_cell(" | ".join(j.badges)),
                "user_status": j.user_status,
                "job_url": safe_csv_cell(j.job_url),
                "teamlyzer_url": safe_csv_cell(cp.teamlyzer_url or ""),
                "fingerprint": j.fingerprint,
            })


def export_json(jobs: list[EnrichedJob], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [j.model_dump(mode="json") for j in jobs]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def export_rejections_csv(rejections: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["source", "company", "title", "location", "reason_code", "reason_detail", "job_url"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rejections:
            writer.writerow({k: safe_csv_cell(row.get(k, "")) for k in fieldnames})


def render_dashboard_html(
    jobs: list[EnrichedJob],
    *,
    interactive: bool = False,
    generated_at: str | None = None,
) -> str:
    """Gera dashboard HTML moderno auto-contido (estático ou ligado ao servidor SQLite local)."""
    jobs_json = json.dumps([j.model_dump(mode="json") for j in jobs], ensure_ascii=False).replace("</", "<\\/")
    gen_str = generated_at or datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")
    mode_flag = "true" if interactive else "false"

    return f"""<!doctype html>
<html lang="pt-PT">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estágio Radar PT — Informática &amp; IA (Setúbal &amp; Lisboa)</title>
<style>
  :root {{
    --bg: #0b1120;
    --surface: #111a2e;
    --surface-2: #17233c;
    --border: #24324f;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --accent-2: #6366f1;
    --green: #10b981;
    --amber: #f59e0b;
    --red: #ef4444;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 60px;
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(circle at top right, #172554 0%, var(--bg) 45%);
    color: var(--text);
    line-height: 1.45;
  }}
  .container {{ max-width: 1180px; margin: 0 auto; }}
  header {{
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    gap: 16px; margin-bottom: 22px;
  }}
  h1 {{ margin: 0; font-size: 26px; letter-spacing: -0.02em; display: flex; align-items: center; gap: 10px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .kpis {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 20px;
  }}
  .kpi {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px;
  }}
  .kpi-val {{ font-size: 24px; font-weight: 800; color: var(--accent); }}
  .kpi-lbl {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .controls {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px; margin-bottom: 18px;
    display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
    gap: 10px; align-items: center;
  }}
  input[type="text"], select {{
    width: 100%; background: var(--surface-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 11px; font-size: 13px; outline: none;
  }}
  input[type="text"]:focus, select:focus {{ border-color: var(--accent); }}
  .toggle {{
    display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer;
    background: var(--surface-2); padding: 8px 11px; border-radius: 8px; border: 1px solid var(--border);
  }}
  .btn {{
    border: none; border-radius: 8px; padding: 9px 14px; font-size: 13px;
    font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
  }}
  .btn:disabled {{ opacity: 0.55; cursor: wait; }}
  .btn-primary {{ background: linear-gradient(135deg, #2563eb, #4f46e5); color: #fff; }}
  .btn-primary:hover {{ filter: brightness(1.1); }}
  .btn-ghost {{ background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }}
  .btn-ghost:hover {{ border-color: var(--accent); }}
  .btn-sm {{ padding: 5px 10px; font-size: 12px; border-radius: 6px; }}
  .btn-active-saved {{ background: #1d4ed8; color: #fff; }}
  .btn-active-applied {{ background: #047857; color: #fff; }}
  .btn-active-interview {{ background: #7c3aed; color: #fff; }}
  .btn-active-ignored {{ background: #991b1b; color: #fff; }}
  .list {{ display: flex; flex-direction: column; gap: 12px; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
    display: flex; flex-wrap: wrap; justify-content: space-between; gap: 14px;
    transition: border-color 0.15s;
  }}
  .card:hover {{ border-color: #3b82f6; }}
  .card.is-ignored {{ opacity: 0.48; }}
  .card-main {{ flex: 1 1 560px; min-width: 0; }}
  .card-top {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .score-pill {{
    font-weight: 800; font-size: 13px; padding: 3px 10px; border-radius: 999px; color: #fff;
  }}
  .s-high {{ background: #059669; }}
  .s-mid {{ background: #2563eb; }}
  .s-low {{ background: #d97706; }}
  .s-dim {{ background: #475569; }}
  .job-title {{
    font-size: 17px; font-weight: 700; color: #f8fafc; text-decoration: none;
  }}
  .job-title:hover {{ color: var(--accent); text-decoration: underline; }}
  .meta {{
    margin-top: 6px; font-size: 13px; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center;
  }}
  .meta strong {{ color: #f1f5f9; }}
  .tl-link {{ color: var(--accent); text-decoration: none; }}
  .tl-link:hover {{ text-decoration: underline; }}
  .badges {{ margin-top: 9px; display: flex; flex-wrap: wrap; gap: 6px; }}
  .badge {{
    font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px;
    background: var(--surface-2); border: 1px solid var(--border); color: #cbd5e1;
  }}
  .badge-warn {{ background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.45); color: #fcd34d; }}
  .badge-new {{ background: rgba(16, 185, 129, 0.18); border-color: rgba(16, 185, 129, 0.5); color: #6ee7b7; }}
  .snippet {{ margin-top: 8px; font-size: 12.5px; color: #94a3b8; max-width: 780px; }}
  .breakdown {{ margin-top: 6px; font-size: 11.5px; color: #64748b; }}
  .card-actions {{
    display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; gap: 10px;
  }}
  .crm-row {{ display: flex; flex-wrap: wrap; gap: 5px; justify-content: flex-end; }}
  .notes-input {{
    margin-top: 8px; width: 100%; max-width: 520px; background: #0f172a; color: #cbd5e1;
    border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; font-size: 12px;
  }}
  .empty {{ text-align: center; padding: 48px 16px; color: var(--muted); background: var(--surface); border-radius: 12px; border: 1px dashed var(--border); }}
  .modal-backdrop {{
    display: none; position: fixed; inset: 0; z-index: 9999;
    background: rgba(2, 6, 23, 0.78); backdrop-filter: blur(4px);
    align-items: center; justify-content: center; padding: 16px;
  }}
  .modal-backdrop.open {{ display: flex; }}
  .modal {{
    width: 100%; max-width: 760px; max-height: 92vh; overflow-y: auto;
    background: var(--surface); border: 1px solid #334155; border-radius: 16px;
    padding: 20px 22px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
  }}
  .modal-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }}
  .modal-title {{ font-size: 18px; font-weight: 700; color: #f8fafc; }}
  .modal-sub {{ font-size: 13px; color: var(--accent); margin-top: 2px; }}
  .cop-box {{
    background: var(--surface-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px; margin-bottom: 12px;
  }}
  .cop-label {{ font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-bottom: 6px; display: block; }}
  textarea.cop-ta, input.cop-inp {{
    width: 100%; background: #0b1120; color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; font-family: inherit;
  }}
  .cop-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
  .cop-status {{ margin-top: 10px; padding: 9px 12px; border-radius: 8px; font-size: 12.5px; display: none; }}
  .cop-status.ok {{ display: block; background: rgba(16, 185, 129, 0.16); border: 1px solid rgba(16, 185, 129, 0.45); color: #6ee7b7; }}
  .cop-status.err {{ display: block; background: rgba(239, 68, 68, 0.16); border: 1px solid rgba(239, 68, 68, 0.45); color: #fca5a5; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>🎯 Estágio Radar PT <span style="font-size:13px;font-weight:600;color:var(--accent);background:var(--surface-2);padding:3px 9px;border-radius:999px;border:1px solid var(--border)">Setúbal · Lisboa · Remoto PT</span></h1>
      <div class="subtitle" id="sub-info">Atualizado em {gen_str} · Zero falsos positivos · Ordenado por Score 0–100</div>
    </div>
    <div id="header-actions" style="display:flex;gap:8px;align-items:center"></div>
  </header>

  <section class="kpis" id="kpis"></section>

  <section class="controls">
    <input type="text" id="q" placeholder="🔍 Pesquisar cargo, empresa, stack (Python, IA, Cloudflare...)">
    <select id="f-zone">
      <option value="">📍 Todas as Zonas</option>
      <option value="SETUBAL_LOCAL">📍 Setúbal / Palmela / Margem Sul</option>
      <option value="LISBOA_METRO">🚆 Lisboa &amp; Grande Lisboa</option>
      <option value="REMOTE_PT">🌐 Remoto (Elegível PT)</option>
      <option value="PT_UNSPECIFIED">🇵🇹 Portugal (Geral)</option>
      <option value="PT_OTHER_CITY">🇵🇹 Outras Cidades PT (--all-portugal)</option>
    </select>
    <select id="f-role">
      <option value="">🎓 Estágios &amp; Entry-Level</option>
      <option value="INTERNSHIP">🎓 Apenas Estágios / Trainees / Grad</option>
      <option value="ENTRY_JUNIOR">🚀 Apenas Junior / Entry-Level</option>
    </select>
    <select id="f-domain">
      <option value="">🧠 Todas as Áreas Tech</option>
      <option value="AI_ML">🤖 IA &amp; Machine Learning</option>
      <option value="DATA">📊 Data Science &amp; Analytics</option>
      <option value="SWE">💻 Software Engineering</option>
      <option value="CYBER">🔐 Cibersegurança</option>
      <option value="CLOUD_DEVOPS">☁️ Cloud / DevOps / SRE</option>
      <option value="EMBEDDED">⚙️ Embedded / Robotics</option>
    </select>
    <select id="f-sort">
      <option value="score">Ordenar: Melhor Score (0–100)</option>
      <option value="fresh">Ordenar: Mais Recentes</option>
      <option value="rating">Ordenar: Rating Teamlyzer</option>
      <option value="salary">Ordenar: Salário Teamlyzer</option>
    </select>
    <label class="toggle">
      <input type="checkbox" id="f-hide-cons">
      <span>Ocultar Consultoras</span>
    </label>
    <label class="toggle">
      <input type="checkbox" id="f-top-tech">
      <span>🏆 Só Top Tech (Tier S/A)</span>
    </label>
  </section>

  <div id="count-bar" style="font-size:13px;color:var(--muted);margin-bottom:10px"></div>
  <main class="list" id="job-list"></main>
</div>

<div class="modal-backdrop" id="apply-modal" onclick="if(event.target===this) closeCopilot()">
  <div class="modal">
    <div class="modal-head">
      <div>
        <div class="modal-title" id="cop-title">Copiloto 1-Clique</div>
        <div class="modal-sub" id="cop-sub"></div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="closeCopilot()">✕ Fechar</button>
    </div>

    <div class="cop-box" id="cop-strategy-box"></div>

    <div class="cop-box" id="cop-tldr-box" style="display:none">
      <span class="cop-label">Resumo da Vaga (TL;DR do Anúncio Real)</span>
      <ul id="cop-tldr" style="margin:0 0 0 18px;font-size:12.5px;color:#e2e8f0;line-height:1.45"></ul>
    </div>

    <div class="cop-box">
      <span class="cop-label">CV Selecionado Automaticamente</span>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <select id="cop-cv-track" style="max-width:340px" onchange="switchCopilotCv(this.value)">
          <option value="ai_ml">Applied AI &amp; ML (PDF)</option>
          <option value="swe">Software Engineering Full-Stack (PDF)</option>
          <option value="enterprise">Enterprise Systems + AI (SAP/Python/Java PDF)</option>
        </select>
        <span id="cop-cv-badge" class="badge badge-new"></span>
      </div>
      <div id="cop-cv-reason" style="font-size:12px;color:var(--muted);margin-top:6px"></div>
    </div>

    <div class="cop-box">
      <span class="cop-label">Pontos Fortes Alinhados à Vaga</span>
      <ul id="cop-highlights" style="margin:0 0 0 18px;font-size:12.5px;color:#cbd5e1;line-height:1.45"></ul>
    </div>

    <div class="cop-box">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <span class="cop-label" style="margin:0">Nota de Apresentação / Cover Note</span>
        <div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-ghost btn-sm" id="tone-balanced" onclick="switchTone('balanced')">Equilibrado</button>
          <button class="btn btn-ghost btn-sm" id="tone-concise" onclick="switchTone('concise')">Mais curto</button>
          <button class="btn btn-ghost btn-sm" id="tone-technical" onclick="switchTone('technical')">Mais técnico</button>
          <button class="btn btn-ghost btn-sm" onclick="regenerateKit()">🔄 Regenerar</button>
          <button class="btn btn-ghost btn-sm" onclick="copyTextField('cop-cover', 'Nota copiada!')">📋 Copiar</button>
        </div>
      </div>
      <textarea id="cop-cover" class="cop-ta" rows="4"></textarea>
    </div>

    <div class="cop-actions">
      <button class="btn btn-primary" id="cop-btn-autofill" onclick="launchAutofill()">🚀 Auto-Preencher &amp; Anexar CV</button>
      <button class="btn btn-ghost" onclick="markAppliedFromCopilot()">✅ Marcar Candidatado</button>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">
      <button class="btn btn-ghost btn-sm" onclick="copyDmAndOpenRecruiters()">💬 Copiar DM + Recrutadores</button>
      <button class="btn btn-ghost btn-sm" onclick="toggleEmailBox()">✉️ Email direto</button>
    </div>

    <div class="cop-status" id="cop-feedback"></div>

    <div class="cop-box" id="cop-email-box" style="display:none;margin-top:12px">
      <span class="cop-label">Envio Direto por Email (com CV PDF anexado)</span>
      <div style="display:flex;flex-direction:column;gap:8px">
        <input id="cop-email-to" class="cop-inp" placeholder="Para (ex: bolsas@inesc-id.pt)">
        <input id="cop-email-subj" class="cop-inp" placeholder="Assunto">
        <textarea id="cop-email-body" class="cop-ta" rows="6"></textarea>
        <div class="cop-actions" style="margin-top:2px">
          <button class="btn btn-primary btn-sm" id="cop-btn-send-email" onclick="sendEmailApplication()">📤 Enviar via SMTP + Marcar</button>
          <a class="btn btn-ghost btn-sm" id="cop-btn-mailto" href="#" target="_blank" rel="noopener">📧 Abrir na App de Email</a>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="gemini-modal" onclick="if(event.target===this) closeGeminiModal()">
  <div class="modal" style="max-width:520px">
    <div class="modal-head">
      <div>
        <div class="modal-title">🔑 Configurar Google Gemini API (Grátis)</div>
        <div class="modal-sub">Redação humana em PT-PT/EN e respostas às perguntas das vagas</div>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="closeGeminiModal()">✕</button>
    </div>
    <div class="cop-box">
      <div style="font-size:12.5px;color:#cbd5e1;line-height:1.5;margin-bottom:10px">
        1. Clica em <strong>Copy key</strong> no <a class="tl-link" href="https://aistudio.google.com/api-keys" target="_blank" rel="noopener">Google AI Studio</a>.<br>
        2. Cola abaixo e guarda — testamos a ligação em direto e fica só no teu <code>.env</code> local.
      </div>
      <input id="gem-key-input" type="password" class="cop-inp" placeholder="Cola aqui a tua GEMINI_API_KEY (AQ... ou AIza...)">
      <div class="cop-actions">
        <button class="btn btn-primary btn-sm" id="btn-save-gemini" onclick="saveGeminiKey()">💾 Testar &amp; Ativar Gemini</button>
      </div>
      <div class="cop-status" id="gem-feedback"></div>
    </div>
  </div>
</div>

<script>
const INTERACTIVE = {mode_flag};
let JOBS = {jobs_json};
let CURRENT_KIT = null;
let CURRENT_TONE = "balanced";

function esc(s) {{
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

function scoreClass(sc) {{
  if (sc >= 82) return "s-high";
  if (sc >= 68) return "s-mid";
  if (sc >= 52) return "s-low";
  return "s-dim";
}}

async function setupInteractiveHeader() {{
  if (!INTERACTIVE) return;
  const wrap = document.getElementById("header-actions");
  wrap.innerHTML =
    '<button class="btn btn-ghost" id="btn-gemini">🔑 Gemini AI</button>' +
    '<button class="btn btn-ghost" id="btn-browser-login" title="Abre o Chrome dedicado do Radar (isolado dos teus 20+ perfis) para entrares 1 vez no LinkedIn e portais">🔐 Login LinkedIn (1x)</button>' +
    '<button class="btn btn-primary" id="btn-scan">⚡ Atualizar Radar Agora</button>';

  try {{
    const pRes = await fetch("/api/profile");
    const pData = await pRes.json();
    const gBtn = document.getElementById("btn-gemini");
    if (pData.gemini_configured) {{
      gBtn.textContent = "✨ Gemini AI Ativo";
      gBtn.className = "btn btn-ghost";
    }} else {{
      gBtn.textContent = "🔑 Ligar Gemini AI";
    }}
  }} catch (_e) {{}}

  document.getElementById("btn-gemini").onclick = () => {{
    document.getElementById("gem-feedback").className = "cop-status";
    document.getElementById("gemini-modal").classList.add("open");
  }};

  document.getElementById("btn-browser-login").onclick = async () => {{
    const b = document.getElementById("btn-browser-login");
    b.disabled = true;
    try {{
      const res = await fetch("/api/browser-login", {{ method: "POST", body: "{{}}" }});
      const data = await res.json();
      alert(data.message || data.error || "Chrome aberto.");
    }} catch (e) {{
      alert("Erro: " + e);
    }} finally {{
      b.disabled = false;
    }}
  }};

  document.getElementById("btn-scan").onclick = async () => {{
    const btn = document.getElementById("btn-scan");
    btn.disabled = true;
    btn.textContent = "⏳ A pesquisar fontes...";
    try {{
      const res = await fetch("/api/scan", {{ method: "POST" }});
      const data = await res.json();
      if (data.jobs) JOBS = data.jobs;
      document.getElementById("sub-info").textContent = "Último scan agora · " + (data.message || "Concluído");
      render();
    }} catch (e) {{
      alert("Erro ao atualizar radar: " + e);
    }} finally {{
      btn.disabled = false;
      btn.textContent = "⚡ Atualizar Radar Agora";
    }}
  }};
}}

function closeGeminiModal() {{
  document.getElementById("gemini-modal").classList.remove("open");
}}

async function saveGeminiKey() {{
  const key = document.getElementById("gem-key-input").value.trim();
  const fb = document.getElementById("gem-feedback");
  const btn = document.getElementById("btn-save-gemini");
  if (!key) {{
    fb.textContent = "Cola uma API Key válida.";
    fb.className = "cop-status err";
    return;
  }}
  btn.disabled = true;
  fb.textContent = "⏳ A testar ligação ao Gemini...";
  fb.className = "cop-status ok";
  try {{
    const res = await fetch("/api/settings/gemini-key", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ api_key: key }})
    }});
    const data = await res.json();
    if (res.ok && data.ok) {{
      fb.textContent = data.message || "Chave validada e guardada!";
      fb.className = "cop-status ok";
      document.getElementById("btn-gemini").textContent = "✨ Gemini AI Ativo";
    }} else {{
      fb.textContent = data.error || "Erro ao validar chave.";
      fb.className = "cop-status err";
    }}
  }} catch (e) {{
    fb.textContent = "Erro de ligação ao servidor local: " + e;
    fb.className = "cop-status err";
  }} finally {{
    btn.disabled = false;
  }}
}}

async function setStatus(fp, status, notes) {{
  const job = JOBS.find(j => j.fingerprint === fp);
  if (job) {{
    job.user_status = status;
    if (notes !== undefined) job.user_notes = notes;
  }}
  render();
  if (INTERACTIVE) {{
    await fetch("/api/status", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ fingerprint: fp, status, notes }})
    }});
  }}
}}

function renderKpis(allJobs) {{
  const internships = allJobs.filter(j => j.role_tier === "INTERNSHIP").length;
  const setLisbon = allJobs.filter(j => j.geo_zone === "SETUBAL_LOCAL" || j.geo_zone === "LISBOA_METRO").length;
  const aiData = allJobs.filter(j => j.domain === "AI_ML" || j.domain === "DATA").length;
  const product = allJobs.filter(j => !j.company_profile?.is_consultancy && ["S","A"].includes(j.company_profile?.company_tier)).length;
  const applied = allJobs.filter(j => j.user_status === "applied" || j.user_status === "interview").length;
  document.getElementById("kpis").innerHTML = `
    <div class="kpi"><div class="kpi-val">${{allJobs.length}}</div><div class="kpi-lbl">Vagas Qualificadas</div></div>
    <div class="kpi"><div class="kpi-val">${{internships}}</div><div class="kpi-lbl">🎓 Estágios / Trainees</div></div>
    <div class="kpi"><div class="kpi-val">${{setLisbon}}</div><div class="kpi-lbl">📍 Setúbal &amp; Lisboa</div></div>
    <div class="kpi"><div class="kpi-val">${{aiData}}</div><div class="kpi-lbl">🤖 IA, ML &amp; Dados</div></div>
    <div class="kpi"><div class="kpi-val">${{product}}</div><div class="kpi-lbl">🏆 Empresas Tier S/A</div></div>
    <div class="kpi"><div class="kpi-val">${{applied}}</div><div class="kpi-lbl">✅ Candidatadas</div></div>
  `;
}}

function render() {{
  renderKpis(JOBS);
  const q = document.getElementById("q").value.trim().toLowerCase();
  const zone = document.getElementById("f-zone").value;
  const role = document.getElementById("f-role").value;
  const domain = document.getElementById("f-domain").value;
  const hideCons = document.getElementById("f-hide-cons").checked;
  const onlyTopTech = document.getElementById("f-top-tech").checked;
  const sortBy = document.getElementById("f-sort").value;

  let filtered = JOBS.filter(j => {{
    if (zone && j.geo_zone !== zone) return false;
    if (role && j.role_tier !== role) return false;
    if (domain && j.domain !== domain) return false;
    if (hideCons && j.company_profile?.is_consultancy) return false;
    if (onlyTopTech && (j.company_profile?.is_consultancy || !["S","A"].includes(j.company_profile?.company_tier))) return false;
    if (q) {{
      const hay = [j.title, j.company, j.location, ...(j.tags || []), ...(j.sources || [])].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }}
    return true;
  }});

  filtered.sort((a, b) => {{
    if (sortBy === "fresh") {{
      return (b.published_at || "").localeCompare(a.published_at || "");
    }}
    if (sortBy === "rating") {{
      return (b.company_profile?.teamlyzer_rating || 0) - (a.company_profile?.teamlyzer_rating || 0) || (b.score - a.score);
    }}
    if (sortBy === "salary") {{
      return (b.company_profile?.avg_salary_eur || 0) - (a.company_profile?.avg_salary_eur || 0) || (b.score - a.score);
    }}
    return b.score - a.score;
  }});

  document.getElementById("count-bar").textContent = `A mostrar ${{filtered.length}} de ${{JOBS.length}} oportunidades`;

  const list = document.getElementById("job-list");
  if (!filtered.length) {{
    list.innerHTML = '<div class="empty">Nenhuma vaga corresponde aos filtros selecionados.</div>';
    return;
  }}

  list.innerHTML = filtered.map(j => {{
    const cp = j.company_profile || {{}};
    const tlTxt = cp.teamlyzer_rating
      ? `★ ${{cp.teamlyzer_rating.toFixed(1)}}/5 (${{cp.teamlyzer_reviews || 0}} rev.)${{cp.avg_salary_eur ? ` · €${{Math.round(cp.avg_salary_eur/1000)}}k méd.` : ""}}`
      : "Sem rating Teamlyzer";
    const tlHtml = cp.teamlyzer_url
      ? `<a class="tl-link" href="${{esc(cp.teamlyzer_url)}}" target="_blank" rel="noopener">${{esc(tlTxt)}}</a>`
      : esc(tlTxt);

    const badgesHtml = (j.badges || []).map(b => {{
      const cls = b.includes("CONSULTORA") ? "badge badge-warn" : (b.includes("NOVO") ? "badge badge-new" : "badge");
      return `<span class="${{cls}}">${{esc(b)}}</span>`;
    }}).join("");

    const bd = j.score_breakdown || {{}};
    const bdLine = `Cargo ${{bd.role_points||0}}/30 · Empresa ${{bd.company_points||0}}/30 · Zona ${{bd.location_points||0}}/20 · Frescura ${{bd.freshness_points||0}}/20`;
    const crmHtml = INTERACTIVE ? `
      <div class="crm-row">
        <button class="btn btn-ghost btn-sm ${{j.user_status==='saved'?'btn-active-saved':''}}" onclick="setStatus('${{j.fingerprint}}','saved')">⭐ Guardar</button>
        <button class="btn btn-ghost btn-sm ${{j.user_status==='applied'?'btn-active-applied':''}}" onclick="setStatus('${{j.fingerprint}}','applied')">✓ Candidatado</button>
        <button class="btn btn-ghost btn-sm ${{j.user_status==='interview'?'btn-active-interview':''}}" onclick="setStatus('${{j.fingerprint}}','interview')">🎤 Entrevista</button>
        <button class="btn btn-ghost btn-sm ${{j.user_status==='ignored'?'btn-active-ignored':''}}" onclick="setStatus('${{j.fingerprint}}','ignored')">✕</button>
      </div>` : "";

    const notesHtml = INTERACTIVE ? `<input class="notes-input" placeholder="Notas pessoais (ex: enviei CV dia 24, contacto...)" value="${{esc(j.user_notes||'')}}" onchange="setStatus('${{j.fingerprint}}','${{j.user_status}}',this.value)">` : "";

    return `
      <article class="card ${{j.user_status==='ignored'?'is-ignored':''}}">
        <div class="card-main">
          <div class="card-top">
            <span class="score-pill ${{scoreClass(j.score)}}">${{Math.round(j.score)}}/100</span>
            <a class="job-title" href="${{esc(j.job_url)}}" target="_blank" rel="noopener">${{esc(j.title)}}</a>
          </div>
          <div class="meta">
            <span><strong>${{esc(j.company)}}</strong></span>
            <span>📍 ${{esc(j.location)}} (${{esc(j.modality)}})</span>
            <span>⏱ ${{esc(j.freshness_label)}} (${{esc(j.published_date)}})</span>
            <span>📊 ${{tlHtml}}</span>
            <span>🔗 ${{esc((j.sources||[]).join(", "))}}</span>
            ${{j.salary ? `<span>💶 ${{esc(j.salary)}}</span>` : ""}}
          </div>
          <div class="badges">${{badgesHtml}}</div>
          ${{j.description_snippet ? `<div class="snippet">${{esc(j.description_snippet.slice(0,220))}}${{j.description_snippet.length>220?"…":""}}</div>` : ""}}
          <div class="breakdown">${{esc(bdLine)}}</div>
          ${{notesHtml}}
        </div>
        <div class="card-actions">
          ${{INTERACTIVE
            ? `<div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
                 <button class="btn btn-primary" onclick="openCopilot('${{j.fingerprint}}')">⚡ 1-Clique CV</button>
                 <a class="btn btn-ghost" href="${{esc(j.job_url)}}" target="_blank" rel="noopener">Abrir →</a>
               </div>`
            : `<a class="btn btn-primary" href="${{esc(j.job_url)}}" target="_blank" rel="noopener">Candidatar →</a>`}}
          ${{crmHtml}}
        </div>
      </article>
    `;
  }}).join("");
}}

function showCopilotFeedback(msg, isErr) {{
  const el = document.getElementById("cop-feedback");
  el.textContent = msg;
  el.className = "cop-status " + (isErr ? "err" : "ok");
}}

function closeCopilot() {{
  document.getElementById("apply-modal").classList.remove("open");
}}

async function openCopilot(fp, overrideTrack, tone, forceRefresh) {{
  const modal = document.getElementById("apply-modal");
  modal.classList.add("open");
  CURRENT_TONE = tone || CURRENT_TONE || "balanced";
  ["balanced", "concise", "technical"].forEach(t => {{
    const el = document.getElementById("tone-" + t);
    if (el) el.className = "btn btn-ghost btn-sm" + (t === CURRENT_TONE ? " btn-active-saved" : "");
  }});

  document.getElementById("cop-feedback").className = "cop-status";
  document.getElementById("cop-email-box").style.display = "none";
  document.getElementById("cop-tldr-box").style.display = "none";
  document.getElementById("cop-title").textContent = "A ler anúncio e preparar candidatura...";
  document.getElementById("cop-sub").textContent = "";

  try {{
    let url = "/api/apply-kit?fingerprint=" + encodeURIComponent(fp) + "&tone=" + encodeURIComponent(CURRENT_TONE);
    if (overrideTrack) url += "&cv_track=" + encodeURIComponent(overrideTrack);
    if (forceRefresh) url += "&refresh=1";

    const res = await fetch(url);
    const kit = await res.json();
    if (!res.ok) {{
      showCopilotFeedback(kit.error || "Erro ao carregar kit.", true);
      return;
    }}
    CURRENT_KIT = kit;
    document.getElementById("cop-title").textContent = kit.natural_role_title || kit.title || "Candidatura";
    const aiBadge = kit.used_ai ? ("✨ " + (kit.ai_model || "Gemini")) : "📝 Modo Offline";
    const extLink = kit.external_apply_url
      ? ` · <a class="tl-link" href="${{esc(kit.external_apply_url)}}" target="_blank" rel="noopener">🔗 Link Direto ATS extraído</a>`
      : "";
    document.getElementById("cop-sub").innerHTML =
      `${{esc(kit.company || "")}} · ${{kit.language === "pt" ? "Português" : "English"}} · ${{esc(aiBadge)}}${{extLink}}`;

    const strat = kit.strategy || {{}};
    document.getElementById("cop-strategy-box").innerHTML =
      `<span class="cop-label">Estratégia Recomendada</span>` +
      `<div style="font-size:13px;font-weight:600;color:#f8fafc">${{esc(strat.label || "")}}</div>` +
      `<div style="font-size:12px;color:var(--muted);margin-top:2px">${{esc(strat.hint || "")}}</div>`;

    const tldr = kit.role_tldr || [];
    if (tldr.length) {{
      document.getElementById("cop-tldr").innerHTML = tldr.map(x => `<li style="margin-bottom:3px">${{esc(x)}}</li>`).join("");
      document.getElementById("cop-tldr-box").style.display = "block";
    }}

    const cv = kit.cv || {{}};
    document.getElementById("cop-cv-track").value = cv.track || "ai_ml";
    const cvBadge = document.getElementById("cop-cv-badge");
    cvBadge.textContent = (cv.exists ? "✅ " : "⚠️ Em falta: ") + (cv.filename || "CV");
    cvBadge.className = "badge " + (cv.exists ? "badge-new" : "badge-warn");
    document.getElementById("cop-cv-reason").textContent = cv.reason || "";

    document.getElementById("cop-highlights").innerHTML = (kit.highlight_bullets || [])
      .map(b => `<li style="margin-bottom:4px">${{esc(b)}}</li>`).join("");

    document.getElementById("cop-cover").value = kit.short_cover_note || "";
    document.getElementById("cop-email-to").value = kit.detected_email || "";
    document.getElementById("cop-email-subj").value = kit.email_subject || "";
    document.getElementById("cop-email-body").value = kit.email_body || "";
    document.getElementById("cop-btn-mailto").href = kit.mailto_url || "#";

    if (kit.detected_email || strat.type === "email_or_portal") {{
      document.getElementById("cop-email-box").style.display = "block";
    }}
  }} catch (err) {{
    showCopilotFeedback("Erro de comunicação local: " + err, true);
  }}
}}

async function switchCopilotCv(track) {{
  if (!CURRENT_KIT) return;
  await openCopilot(CURRENT_KIT.fingerprint, track, CURRENT_TONE, false);
}}

async function switchTone(tone) {{
  if (!CURRENT_KIT) return;
  CURRENT_TONE = tone;
  await openCopilot(CURRENT_KIT.fingerprint, document.getElementById("cop-cv-track").value, tone, false);
}}

async function regenerateKit() {{
  if (!CURRENT_KIT) return;
  await openCopilot(CURRENT_KIT.fingerprint, document.getElementById("cop-cv-track").value, CURRENT_TONE, true);
}}

async function copyTextField(id, okMsg) {{
  const val = document.getElementById(id).value;
  try {{
    await navigator.clipboard.writeText(val);
    showCopilotFeedback(okMsg, false);
  }} catch (e) {{
    document.getElementById(id).select();
    showCopilotFeedback("Texto selecionado — usa Ctrl+C.", false);
  }}
}}

let AUTOFILL_POLL_TIMER = null;

async function pollAutofillStatus(attemptsLeft) {{
  if (attemptsLeft <= 0) return;
  try {{
    const res = await fetch("/api/autofill-status");
    const st = await res.json();
    const s = st.status || "idle";
    if (s === "done") {{
      showCopilotFeedback(st.message || "✅ Auto-preenchido! Revê e submete no Chrome dedicado.", false);
      return;
    }}
    if (s === "needs_login" || s === "captcha_blocked") {{
      showCopilotFeedback(st.message || "⚠️ Ação necessária na janela do Chrome dedicado.", false);
      return;
    }}
    if (s === "error") {{
      showCopilotFeedback(st.message || st.error || "Erro no Chrome dedicado.", true);
      return;
    }}
    if (st.message) {{
      showCopilotFeedback("⏳ " + st.message, false);
    }}
  }} catch (_e) {{}}
  AUTOFILL_POLL_TIMER = setTimeout(() => pollAutofillStatus(attemptsLeft - 1), 1000);
}}

async function launchAutofill() {{
  if (!CURRENT_KIT) return;
  if (AUTOFILL_POLL_TIMER) {{
    clearTimeout(AUTOFILL_POLL_TIMER);
    AUTOFILL_POLL_TIMER = null;
  }}
  const btn = document.getElementById("cop-btn-autofill");
  btn.disabled = true;
  showCopilotFeedback("⏳ A preencher formulário e anexar CV no Chrome dedicado...", false);
  try {{
    const res = await fetch("/api/autofill", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        fingerprint: CURRENT_KIT.fingerprint,
        cv_track: document.getElementById("cop-cv-track").value,
        tone: CURRENT_TONE,
        cover_note: document.getElementById("cop-cover").value,
        language: CURRENT_KIT.language || "en"
      }})
    }});
    const data = await res.json();
    if (res.ok && data.ok) {{
      showCopilotFeedback(data.message || "Chrome dedicado a preencher! A acompanhar...", false);
      AUTOFILL_POLL_TIMER = setTimeout(() => pollAutofillStatus(35), 900);
    }} else {{
      showCopilotFeedback(data.error || "Erro ao lançar Chrome dedicado.", true);
    }}
  }} catch (e) {{
    showCopilotFeedback("Erro ao contactar servidor: " + e, true);
  }} finally {{
    btn.disabled = false;
  }}
}}

async function copyDmAndOpenRecruiters() {{
  if (!CURRENT_KIT) return;
  try {{
    await navigator.clipboard.writeText(CURRENT_KIT.recruiter_dm || "");
    showCopilotFeedback("Mensagem DM copiada para o clipboard! A abrir recrutadores no LinkedIn...", false);
  }} catch (e) {{
    showCopilotFeedback("A abrir pesquisa de recrutadores no LinkedIn...", false);
  }}
  window.open(CURRENT_KIT.recruiter_search_url, "_blank", "noopener");
}}

function toggleEmailBox() {{
  const box = document.getElementById("cop-email-box");
  box.style.display = box.style.display === "none" ? "block" : "none";
}}

async function sendEmailApplication() {{
  if (!CURRENT_KIT) return;
  const toEmail = document.getElementById("cop-email-to").value.trim();
  const subject = document.getElementById("cop-email-subj").value.trim();
  const body = document.getElementById("cop-email-body").value;
  const cvTrack = document.getElementById("cop-cv-track").value;
  const btn = document.getElementById("cop-btn-send-email");
  btn.disabled = true;
  try {{
    const res = await fetch("/api/apply-email", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{
        fingerprint: CURRENT_KIT.fingerprint,
        to_email: toEmail,
        subject,
        body,
        cv_track: cvTrack
      }})
    }});
    const data = await res.json();
    if (res.ok && data.ok) {{
      showCopilotFeedback(data.message, false);
      const today = new Date().toLocaleDateString("pt-PT");
      const cvName = (CURRENT_KIT.cv && CURRENT_KIT.cv.filename) || "CV";
      await setStatus(CURRENT_KIT.fingerprint, "applied", `Email enviado para ${{toEmail}} (${{cvName}}) em ${{today}}`);
    }} else {{
      showCopilotFeedback(data.error || "Não foi possível enviar via SMTP (usa 'Abrir na App de Email').", true);
    }}
  }} catch (e) {{
    showCopilotFeedback("Erro ao enviar email: " + e, true);
  }} finally {{
    btn.disabled = false;
  }}
}}

async function markAppliedFromCopilot() {{
  if (!CURRENT_KIT) return;
  const today = new Date().toLocaleDateString("pt-PT");
  const cvName = (CURRENT_KIT.cv && CURRENT_KIT.cv.filename) || "CV";
  await setStatus(CURRENT_KIT.fingerprint, "applied", `Candidatura enviada com ${{cvName}} em ${{today}}`);
  showCopilotFeedback("Vaga marcada como Candidatada!", false);
}}

["q","f-zone","f-role","f-domain","f-sort","f-hide-cons","f-top-tech"].forEach(id => {{
  document.getElementById(id).addEventListener("input", render);
  document.getElementById(id).addEventListener("change", render);
}});
setupInteractiveHeader();
render();
</script>
</body>
</html>"""


def export_all_artifacts(
    jobs: list[EnrichedJob],
    rejections: list[dict[str, Any]],
    output_dir: str | Path = "output",
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "estagios_radar.csv"
    json_path = out / "estagios_radar.json"
    rej_path = out / "rejections_audit.csv"
    dash_path = out / "dashboard.html"

    export_csv(jobs, csv_path)
    export_json(jobs, json_path)
    export_rejections_csv(rejections, rej_path)
    dash_path.write_text(render_dashboard_html(jobs, interactive=False), encoding="utf-8")

    return {
        "csv": csv_path,
        "json": json_path,
        "rejections": rej_path,
        "dashboard": dash_path,
    }
