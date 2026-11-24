# 🎯 Estágio Radar PT v4.0 — Modo Sniper ao Minuto

**Radar first-mover de alta precisão para estágios e oportunidades entry-level em Informática, Inteligência Artificial, Dados e Engenharia de Software em Portugal** — desenhado especificamente para quem vive em **Setúbal** e procura oportunidades em **Setúbal, Palmela, Margem Sul, Lisboa e Remoto elegível em Portugal**.

---

## O que traz a versão 4.0 (Modo Sniper + Cobertura Total)

- **⚡ Modo Sniper ao Minuto (`--mode sniper`)**: corre em poucos segundos consultando **75+ quadros ATS diretos** (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee, Teamtailor, Workday, **Siemens Avature**, **EDP/Miniclip/Galp SuccessFactors**, **Nokia Oracle HCM**) com **cache HTTP ETag (`304 Not Modified`)** + **LinkedIn janela 1h (`f_TPR=r3600`)** + Teamlyzer/ITJobs.
- **🎯 Trackers Europeus + Bolsas I&D Portugal (`--mode full`)**: integra **`cavlero/Tech-Internships-Europe`**, **`Emjumaev/FAANG-2027-Internships-Tracker-Europe`** e **EURAXESS Portugal** (Bolsas de Investigação em IA/Informática no IST, INESC-ID, FCT-NOVA Caparica, FCUL).
- **📍 Filtro Setúbal/Lisboa Cirúrgico (ou `--all-portugal`)**: por defeito foca-se em Setúbal, Palmela, Margem Sul, Grande Lisboa e Remoto PT (e bloqueia EUA/Ásia e falsos positivos como `internal`/`international` ou vagas Mid/Senior). Se quiseres espreitar também Braga/Aveiro/Porto, passa `--all-portugal`.
- **📱 Alertas Instantâneos Apenas para Novidades**: via **ntfy.sh** (grátis no telemóvel), **Discord**, **Telegram** e **Email SMTP** apoiados por persistência SQLite.

---

## Instalação Rápida (Windows PowerShell)

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
```

*(Ou com Python padrão: `py -3.12 -m venv .venv` e `.venv\Scripts\python.exe -m pip install -e ".[dev]"`.)*

---

## Como Usar

### 1. Recolha Completa Agora (Deep Sweep)
```powershell
.\.venv\Scripts\python.exe -m estagio_radar scan --mode full
```
*(Opcional: adiciona `--all-portugal` para incluir Braga/Aveiro/Porto ou `--hide-consultancies` para ocultar consultoras.)*

### 2. Modo Sniper Contínuo (Ao Minuto — Recomendado!)
Corre automaticamente ATS diretos + LinkedIn 1h a cada **5 minutos** (com cache ETag 304) e faz um Deep Sweep a cada **60 minutos**, enviando push instantâneo para o teu telemóvel mal abre uma vaga nova:
```powershell
.\.venv\Scripts\python.exe -m estagio_radar watch --sniper-interval 5 --deep-interval 60 --notify
```
Ou instala automaticamente como tarefa em segundo plano no Windows:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -IntervalMinutes 5
```

### 3. Abrir o Dashboard Interativo Local
```powershell
.\.venv\Scripts\python.exe -m estagio_radar ui
```
Abre `http://127.0.0.1:8765` com filtros instantâneos, toggle **"🏆 Só Top Tech (Tier S/A)"**, botões **Guardar / Candidatado ✓ / Entrevista**, notas pessoais em SQLite e atualização ao vivo.

---

## Ativar Notificações no Telemóvel (1 minuto)
1. Copia [`.env.example`](.env.example) para `.env`.
2. Instala a app gratuita **ntfy** no telemóvel, subscreve um tópico secreto único (ex: `estagios-setubal-rui-9821`) e coloca `NTFY_TOPIC=estagios-setubal-rui-9821` no `.env`.
3. Opcionalmente configura também `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` ou `SMTP_*`.

---

## Personalizar Empresas & Fontes
- **Adicionar empresas ou consultoras**: edita [`config/companies.yaml`](config/companies.yaml).
- **Adicionar quadros ATS diretos**: edita [`config/sources.yaml`](config/sources.yaml).
