# Candidaturas AI Scraper

[![CI](https://github.com/ruimiguelyo/candidaturas-ai-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/ruimiguelyo/candidaturas-ai-scraper/actions/workflows/ci.yml)
[![Daily scraper](https://github.com/ruimiguelyo/candidaturas-ai-scraper/actions/workflows/daily_scraper.yml/badge.svg)](https://github.com/ruimiguelyo/candidaturas-ai-scraper/actions/workflows/daily_scraper.yml)

Agregador pessoal de vagas técnicas **Junior, Trainee, Graduate e Internship**. Recolhe anúncios de várias fontes, aplica regras determinísticas de relevância, cruza ratings de empresas e produz um CSV/JSON público mais um digest privado opcional.

O projeto **não se candidata automaticamente**, não envia mensagens para recrutadores e não usa um LLM para inventar adequação. Os contactos encontrados por pesquisa pública são sempre marcados como pendentes de verificação.

## O que melhorou na versão 2

- Recolhas vazias, demasiado pequenas ou sem diversidade de fontes interrompem a execução e preservam o último dataset válido.
- O terminal mostra saúde por fonte: pedidos bem-sucedidos, parciais, falhas e vagas brutas.
- Email e pesquisa de contactos são ações explícitas; uma execução local normal não envia nada.
- O digest diário compara o snapshot anterior e envia apenas vagas novas; `--notify-all` continua disponível para uma revisão completa.
- O dataset público já não contém perfis pessoais, mensagens, inferências de outreach ou corpos extensos de descrições.
- Células CSV potencialmente interpretadas como fórmulas são neutralizadas.
- Resultados de LinkedIn e Glassdoor só são associados a uma empresa quando o nome está realmente presente na evidência.
- Rascunhos nunca afirmam que uma candidatura foi submetida sem esse estado existir.
- Funções claramente não técnicas de marketing, vendas e direito, bem como títulos mistos Júnior/Sénior, são excluídas. Termos ambíguos como `operations`, `content` e `manager` deixaram de bloquear cargos técnicos por si só.
- Cada vaga explicita `location_compatibility` e notas; “remote” nunca é convertido silenciosamente em elegibilidade para Portugal. A localização informa a decisão, mas já não elimina ofertas.
- Senioridade entry-level pode ser demonstrada pelo título, metadata da fonte ou descrição, incluindo `Associate`, `New Grad`, `Apprentice`, `Academy`, `Level I` e experiência de 0–2 anos.
- Frontend, mobile, cybersecurity, Python, platform, SRE, embedded, product, web, application e test/automation engineering fazem parte das áreas técnicas reconhecidas.
- Ratings Teamlyzer/Glassdoor servem apenas para ordenar; uma vaga nunca é eliminada por falta de rating ou por uma nota baixa.
- As rejeições do filtro central e a consolidação de duplicados ficam em `vagas_rejeitadas.csv`, com etapa e motivo auditáveis. A Deloitte continua explicitamente excluída.
- Dependências diretas estão fixadas; CI corre offline em Windows e Linux, Python 3.10 e 3.12.
- O workflow diário separa os segredos SMTP da única etapa com permissão de escrita no GitHub.

## Fontes

- LinkedIn Jobs (página pública para visitantes)
- ITJobs.pt
- Landing.jobs
- Himalayas
- Jobicy
- Arbeitnow
- RemoteOK
- Teamlyzer e Glassdoor para reputação da empresa
- Pesquisa pública DuckDuckGo, opcional, para sugerir contactos a verificar

As páginas e endpoints de terceiros podem mudar, impor limites ou devolver dados incompletos. Consulta os termos de cada fonte e usa limites responsáveis.

## Instalação

Requer Python 3.10 a 3.12.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --editable .
candidaturas --help
```

### macOS/Linux

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --editable .
candidaturas --help
```

## Utilização segura

Executar e gravar os resultados no diretório atual, sem email e sem pesquisa de contactos:

```bash
candidaturas
```

Escolher outro diretório:

```bash
candidaturas --output-dir ./output
```

Ativar explicitamente o enriquecimento privado:

```bash
cp candidate_profile.example.json candidate_profile.local.json
# Edita apenas com factos verificáveis sobre o teu perfil.
candidaturas --outreach
```

Enviar apenas as vagas novas exige configuração SMTP e a flag explícita:

```bash
candidaturas --notify
```

Para reenviar o snapshot completo:

```bash
candidaturas --notify-all
```

O ficheiro [`.env.example`](.env.example) documenta as variáveis aceites. O programa não carrega `.env` automaticamente; exporta as variáveis no shell ou configura-as como GitHub Actions secrets.

## Privacidade e estado da evidência

- `candidate_profile.local.json` é ignorado pelo Git e deve permanecer privado.
- `candidate_profile.example.json` contém apenas placeholders.
- `human_outreach` e `description_snippet` existem apenas em memória durante a execução.
- Os exports públicos não incluem nomes de contactos, URLs pessoais, hooks ou mensagens.
- Um contacto sugerido tem `verification_status=PENDING` e `outreach_recommendation=VERIFY_FIRST`.
- “Confiança” mede apenas a correspondência do resultado de pesquisa; não confirma que a pessoa contrata para aquela vaga.

Se a versão antiga do repositório já publicou dados pessoais, removê-los do ficheiro atual não os apaga do histórico Git. Avalia reescrever o histórico apenas se isso for necessário e depois roda credenciais que tenham sido expostas.

## Outputs

| Ficheiro | Conteúdo | Público |
|---|---|---|
| `vagas_estritamente_junior_trainee_internship.json` | Registos normalizados e ordenados | Sim |
| `vagas_estritamente_junior_trainee_internship.csv` | Vista compatível com folhas de cálculo | Sim |
| `vagas_rejeitadas.csv` | Vagas únicas rejeitadas pelo filtro central e grupos de duplicados consolidados | Sim |
| `company_scores_cache.json` | Cache de ratings e respetiva evidência | Sim |
| Digest HTML enviado por SMTP | Vagas e, se ativados, contactos por verificar | Não é persistido |

Os ratings ajudam a ordenar e não constituem recomendação definitiva. Vagas de qualquer categoria permanecem visíveis mesmo quando a empresa não tem rating verificável.

## Pipeline

```text
fontes públicas
    ↓ pesquisas alargadas + paginação + saúde por fonte
normalização e sinais entry-level no título, metadata ou descrição
    ↓ classificação técnica + deduplicação + auditoria das rejeições
rating Teamlyzer → fallback Glassdoor validado
    ↓ ordenação informativa, sem excluir por rating/localização
outreach privado opcional e não confirmado
    ├─ digest SMTP explícito
    └─ export público sanitizado e atómico
```

## Testes e qualidade

```bash
python -m pip install --requirement requirements-dev.txt
python -m ruff check .
python -m unittest discover -s tests -p "test_*.py" -v
python -m coverage run -m unittest discover -s tests -p "test_*.py"
python -m coverage report
```

A suite cobre regras de negócio, motivos de rejeição, paginação, falhas totais e parciais, privacidade dos exports, injeção de fórmulas CSV, correspondência de empresas, ordenação e HTML do digest. Os testes normais não dependem da rede.

## GitHub Actions

Configura estes secrets para o digest diário:

- `SMTP_USER`
- `SMTP_PASS`
- `RECEIVER_EMAIL`
- `SMTP_SERVER` (opcional; Gmail por omissão)
- `SMTP_PORT` (opcional; `587` por omissão)
- `CANDIDATE_PROFILE_JSON` (opcional; conteúdo integral do perfil privado)

O job que acede aos secrets tem apenas `contents: read` e faz checkout sem credenciais persistentes. Apenas um segundo job, sem secrets SMTP e depois de validar os artefactos, recebe `contents: write` para publicar os três ficheiros públicos.

## Limitações atuais

- Datas ainda chegam em formatos diferentes conforme a fonte.
- “Remote” pode estar limitado a um país; confirma sempre autorização de trabalho e localização.
- A deduplicação entre plataformas ainda é conservadora e pode manter anúncios sindicados.
- As pesquisas foram alargadas, mas nenhum conjunto de termos ou limite de um portal garante cobertura integral do mercado.
- A auditoria começa quando uma fonte entrega um anúncio normalizado ao pipeline: não representa anúncios que o portal não devolveu, limites de pesquisa, falhas de rede ou itens que o parser não conseguiu normalizar.
- Scraping HTML é inerentemente frágil; alterações nos portais devem resultar numa falha observável e num ajuste do respetivo adapter.
- Não existe licença definida. Escolhe uma conscientemente antes de incentivar reutilização externa.
