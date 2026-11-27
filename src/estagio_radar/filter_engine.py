"""Motor de filtragem rigoroso com zero falsos positivos."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Iterable

from estagio_radar.date_utils import recency_check
from estagio_radar.geo import classify_geo
from estagio_radar.models import DomainCategory, FilterDecision, GeoZone, RawJob, RoleTier

# Excluir rigorosamente níveis Sénior / Mid / Lead no título ou senioridade nativa
SENIOR_MID_PATTERNS = [
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\blead\b",
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\bdirector\b",
    r"\bdiretor\b",
    r"\bhead\s+of\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bchief\b",
    r"\bmid[- ]?level\b",
    r"\bmidweight\b",
    r"\bmid\s*/\s*senior\b",
    r"\bsenior\s*/\s*mid\b",
    r"\bpleno\b",
    r"\bjr\s*/\s*pl\b",
    r"\bjr\s*/\s*mid\b",
    r"\bjunior\+{2,}\b",
    r"\b(?:engineer|developer|technician|analyst|scientist)\s+(?:ii|iii|iv)\b",
    r"\bexperienced\b",
    r"\barchitect\b",
    r"\bmanager\b",
    r"\bgestor(?:a)?\b",
    r"\bgerente\b",
    r"\bassistant\s+professor\b",
    r"\bprofessor\s+(?:adjunto|auxiliar|associado|catedratico)\b",
    r"\bpostdoc(?:toral)?\b",
    r"\bpos[- ]doutoramento\b",
    r"\bbipd\b",
    r"\bphd\s+(?:students?|position|fellowship|candidate)\b",
    r"\bbolsa\s+de\s+doutoramento\b",
    r"\bdoctoral\b",
    r"\btenure[- ]track\b",
]

# Funções não técnicas / fora da área de Informática & IA
NON_TECH_PATTERNS = [
    r"\bsales\b",
    r"\bbusiness\s+development\b",
    r"\b(?:bdr|sdr)\b",
    r"\baccount\s+(?:executive|manager|receivable|payable|representative)\b",
    r"\bcustomer\s+(?:experience|support|success|service)\b",
    r"\bpayroll\b",
    r"\baudit(?:or|ing)?\b",
    r"\btax\b",
    r"\baccountant\b",
    r"\baccounting\b",
    r"\bcontabilidade\b",
    r"\bfinance\b",
    r"\bfinancial\b",
    r"\blegal\b",
    r"\bcounsel\b",
    r"\blawyer\b",
    r"\badvogad[oa]\b",
    r"\bparalegal\b",
    r"\bregulatory\s+affairs\b",
    r"\bhuman\s+resources\b",
    r"\bhr\b",
    r"\brecursos\s+humanos\b",
    r"\bpeople\s+(?:operations|partner|team)\b",
    r"\btalent\s+acquisition\b",
    r"\brecruit(?:er|ment|ing)\b",
    r"\bsourcer\b",
    r"\bmarketing\b",
    r"\bcommunications?\b",
    r"\bcomunicacao\b",
    r"\bcontent\s+(?:enablement|creator|strategist|writer|marketing)\b",
    r"\bcopywriter\b",
    r"\bsocial\s+media\b",
    r"\bpublic\s+relations\b",
    r"\bproduct\s+design(?:er)?\b",
    r"\bux\s*/?\s*ui\b",
    r"\bgraphic\s+design\b",
    r"\bmultimedia\b",
    r"\baudiovisual\b",
    r"\breal\s+estate\b",
    r"\bimobiliari[oa]\b",
    r"\bengenheir[oa]\s+civil\b",
    r"\bcivil\s+engineer\b",
    r"\bmechanical\s+engineer\b",
    r"\bengenheir[oa]\s+mecanic[oa]\b",
    r"\bchemical\s+engineer\b",
    r"\badvertising\b",
    r"\badministrative\s+assistant\b",
    r"\bassistente\s+administrativ[oa]\b",
    r"\bcomercial\b",
    r"\brent-a-car\b",
    r"\bstore\s+(?:manager|assistant)\b",
    r"\bretail\b",
    r"\bwarehouse\b",
    r"\blogistica\b",
    r"\bnurse\b",
    r"\bmedical\b",
    r"\bclinical\b",
    r"\bdeal\s+intelligence\b",
    r"\binvestment\s+banking\b",
]

# Sinais explícitos de ESTÁGIO / TRAINEE / GRADUATE (NEVER match "internal" or "international")
INTERNSHIP_PATTERNS = [
    r"\bintern\b",
    r"\binterns\b",
    r"\binternship\b",
    r"\binternships\b",
    r"\bestagio\b",
    r"\bestagios\b",
    r"\bestagiari[oa]s?\b",
    r"\btrainee\b",
    r"\btrainees\b",
    r"\bgraduate\s+(?:programme|program|engineer|developer|software|data|ai|scheme|tech|analyst)\b",
    r"\bnew[- ]grad(?:uate)?s?\b",
    r"\bco[- ]op\b",
    r"\bworking\s+student\b",
    r"\bsummer\s+intern\b",
    r"\bwinter\s+intern\b",
    r"\bspring\s+intern\b",
    r"\biefp\b",
    r"\bbolseir[oa]\b",
    r"\bbolsa\s+de\s+investigacao\b",
    r"\bresearch\s+fellowship\b",
    r"\bresearch\s+grant\b",
    r"\bfellowship\s+for\s+(?:bachelors?|masters?)\b",
    r"\bapprentic(?:e|eship)\b",
    r"\bbeapro\b",
    r"\bacademy\b",
]

# Sinais explícitos de ENTRY-LEVEL / JUNIOR (0-1 ano)
JUNIOR_PATTERNS = [
    r"\bjunior\b",
    r"\bjr\.?\b",
    r"\bentry[- ]level\b",
    r"\bearly[- ]career\b",
    r"\brecem[- ]licenciad[oa]\b",
    r"\bgraduate\b",
    r"\bassociate\s+(?:software|data|ai|ml|cloud|security|qa|systems|devops|developer|engineer|consultant)\b",
    r"\b(?:engineer|developer)\s+i\b",
    r"\blevel\s*(?:1|i)\b",
]

# Sinais na descrição que provam vaga de estágio / 0 experiência quando o título tem só "Junior" ou similar
DESC_DISQUALIFYING_EXPERIENCE_RE = re.compile(
    r"(?:"
    r"(?:minimum|at\s+least|minimo|experi[eê]ncia\s+m[ií]nima\s+de)\s+(?:of\s+)?([2-9]|1\d)\+?\s*(?:years?|anos?)"
    r"|"
    r"\b([3-9]|1\d)\+\s*(?:years?|anos?)\s+(?:of\s+)?(?:professional\s+|industry\s+|relevant\s+|work\s+)?(?:experience|experiencia)"
    r"|"
    r"\b([3-9]|1\d)\s*(?:-|to|a)\s*(?:[4-9]|1\d)\s*(?:years?|anos?)\s+(?:of\s+)?(?:experience|experiencia)"
    r")",
    re.IGNORECASE,
)

# Domínios técnicos
AI_ML_PATTERNS = [
    r"\bai\b", r"\bia\b", r"\bmachine\s+learning\b", r"\bml\b", r"\bdeep\s+learning\b",
    r"\bartificial\s+intelligence\b", r"\binteligencia\s+artificial\b", r"\bllm\b", r"\bllms\b",
    r"\bgenerative\s+ai\b", r"\bgen[- ]?ai\b", r"\bnlp\b", r"\bnatural\s+language\s+processing\b",
    r"\bcomputer\s+vision\b", r"\bvisao\s+computacional\b", r"\brag\b", r"\bagentic\b",
    r"\bmlops\b", r"\bprompt\s+engineer", r"\bapplied\s+scientist\b", r"\bresearch\s+scientist\b",
    r"\btransformers?\b",
]

DATA_PATTERNS = [
    r"\bdata\s+scien", r"\bdata\s+engineer", r"\bdata\s+analyst", r"\bdata\s+analytics?\b",
    r"\bciencia\s+de\s+dados\b", r"\bcientista\s+de\s+dados\b", r"\bengenheir[oa]\s+de\s+dados\b",
    r"\banalista\s+de\s+dados\b", r"\banalytics\b", r"\bbusiness\s+intelligence\b",
    r"\bbi\s+(?:analyst|developer|engineer|consultant|intern|trainee)\b",
    r"\bdata\s+platform\b", r"\bdata\s+intern\b", r"\bsql\b", r"\bpower\s*bi\b", r"\btableau\b",
    r"\betl\b", r"\bdatabricks\b", r"\bsnowflake\b",
]

CYBER_PATTERNS = [
    r"\bcyber[- ]?security\b", r"\bciberseguranca\b", r"\binformation\s+security\b",
    r"\bseguranca\s+(?:da\s+informacao|informatica)\b", r"\bsecurity\s+(?:engineer|analyst|intern|operations)\b",
    r"\bappsec\b", r"\bdevsecops\b", r"\bpentest", r"\bsoc\b", r"\biam\b", r"\binfosec\b",
    r"\bcloud\s+security\b",
]

CLOUD_DEVOPS_PATTERNS = [
    r"\bcloud\b", r"\bdevops\b", r"\bsre\b", r"\bsite\s+reliability\b",
    r"\bplatform\s+engineer", r"\bkubernetes\b", r"\binfrastructure\b", r"\baws\b",
    r"\bazure\b", r"\bgcp\b", r"\bnetwork\s+engineer", r"\bsystems?\s+engineer",
    r"\bsistemas?\s+e\s+redes\b", r"\blinux\b", r"\bterraform\b",
]

EMBEDDED_PATTERNS = [
    r"\bembedded\b", r"\bembebidos\b", r"\bfirmware\b", r"\biot\b", r"\brobotics\b",
    r"\bautomotive\s+software\b", r"\bfpga\b", r"\brtos\b", r"\bc\+\+\b",
]

SWE_PATTERNS = [
    r"\bsoftware\b", r"\binformatica\b", r"\bcomputer\s+science\b", r"\bdeveloper\b",
    r"\bdesenvolvedor", r"\bprogramador", r"\bprogramacao\b", r"\bfull[- ]?stack\b",
    r"\bfront[- ]?end\b", r"\bback[- ]?end\b", r"\bbackend\b", r"\bfrontend\b",
    r"\bweb\s+(?:developer|engineer|development)\b", r"\bmobile\b", r"\bios\b", r"\bandroid\b",
    r"\bpython\b", r"\bjava\b", r"\bjavascript\b", r"\btypescript\b", r"\breact\b",
    r"\bangular\b", r"\bgolang\b", r"\brust\b", r"\b\.net\b", r"\bc#\b", r"\bphp\b",
    r"\bqa\b", r"\bquality\s+assurance\b", r"\btest\s+automation\b", r"\bqa\s+automation\b",
    r"\btester\b", r"\bautomation\s+engineer", r"\brpa\b", r"\bit\s+(?:engineer|intern|trainee|graduate)\b",
    r"\btecnologias?\s+(?:da|de)\s+informacao\b", r"\bapplication\s+(?:developer|engineer)\b",
    r"\btechnology\s+graduate\b", r"\bresearch\s+engineer\b", r"\bproduct\s+engineer\b",
    r"\bsap\b", r"\berp\b", r"\bsystems?\s+analyst\b", r"\banalista\s+de\s+sistemas\b",
    r"\bhelpdesk\b", r"\bsuporte\s+informatico\b",
    r"\bapplication\s+manager\b", r"\bdigital\s+solutions\b", r"\bautomation\b",
    r"\bit\s+systems\b", r"\btechnology\s+services\b", r"\btech\b",
]


def _ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _match_any(text: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def _allow_tech_override(title_ascii: str, matched_non_tech: str) -> bool:
    """Evita falsos bloqueios quando o termo aparece num cargo claramente tech/AI."""
    low = matched_non_tech.casefold()
    if low == "finance" and re.search(r"\b(?:data|ai|ml|quant|software|engineer|developer)\b", title_ascii, re.I):
        return True
    if low == "sales" and (
        re.search(r"\b(?:intern(?:ships?)?|estagios?|trainees?)\b", title_ascii, re.I)
        and re.search(r"\b(?:ai|ia|ml|software|cloud|cyber|engineering|developer|solutions)\b", title_ascii, re.I)
    ):
        return True
    if low in {"comercial", "marketing"} and re.search(r"\b(?:data|ai|ml|software)\s+(?:engineer|scientist|analyst)\b", title_ascii, re.I):
        return True
    if low == "clinical" and re.search(r"\b(?:data|ai|ml|bioinformatics|software)\b", title_ascii, re.I):
        return True
    return False


def _allow_senior_override(title_ascii: str, matched_senior: str) -> bool:
    low = matched_senior.casefold()
    if low == "architect" and re.search(r"\b(?:intern|estagio|trainee)\b", title_ascii, re.I):
        return True
    if low == "manager" and (
        re.search(r"\b(?:intern(?:ships?)?|estagios?|trainees?)\b", title_ascii, re.I)
        and re.search(r"\b(?:application|it|software|ai|ia|ml|data|cloud|systems|cyber|infrastructure|digital|automation|product|program|technical)\b", title_ascii, re.I)
    ):
        return True
    return False


def classify_domain(title_ascii: str, tags_ascii: str) -> DomainCategory | None:
    """Classifica o domínio técnico com prioridade ao título antes das tags."""
    # Primeiro verifica o título (evita que uma tag promocional "AI" transforme QA em AI)
    for domain, pats in (
        (DomainCategory.AI_ML, AI_ML_PATTERNS),
        (DomainCategory.DATA, DATA_PATTERNS),
        (DomainCategory.CYBER, CYBER_PATTERNS),
        (DomainCategory.CLOUD_DEVOPS, CLOUD_DEVOPS_PATTERNS),
        (DomainCategory.EMBEDDED, EMBEDDED_PATTERNS),
        (DomainCategory.SWE, SWE_PATTERNS),
    ):
        if _match_any(title_ascii, pats):
            return domain

    # Fallback nas tags
    for domain, pats in (
        (DomainCategory.AI_ML, AI_ML_PATTERNS),
        (DomainCategory.DATA, DATA_PATTERNS),
        (DomainCategory.CYBER, CYBER_PATTERNS),
        (DomainCategory.CLOUD_DEVOPS, CLOUD_DEVOPS_PATTERNS),
        (DomainCategory.EMBEDDED, EMBEDDED_PATTERNS),
        (DomainCategory.SWE, SWE_PATTERNS),
    ):
        if _match_any(tags_ascii, pats):
            return domain

    return None


def evaluate_raw_job(
    job: RawJob,
    *,
    hard_excluded: Iterable[str] = ("deloitte",),
    recency_days: int | None = None,
    now: datetime | None = None,
    include_all_portugal: bool = False,
    extra_tech_company_override: bool = False,
) -> FilterDecision:
    """Avalia uma vaga bruta e devolve decisão estruturada e auditável."""
    title_ascii = _ascii(job.title).strip()
    company_ascii = _ascii(job.company).strip()
    seniority_ascii = _ascii(job.seniority or "").strip()
    emp_type_ascii = _ascii(job.employment_type or "").strip()
    tags_ascii = _ascii(" ".join(job.tags or []))
    desc_ascii = _ascii(job.description or "")

    # 1. Exclusões explícitas de empresa
    for excluded in hard_excluded:
        ex_clean = _ascii(excluded).casefold().strip()
        if ex_clean and (
            re.search(rf"\b{re.escape(ex_clean)}\b", company_ascii, re.I)
            or re.search(rf"\b{re.escape(ex_clean)}\b", title_ascii, re.I)
        ):
            return FilterDecision(
                accepted=False,
                reason_code="excluded_company",
                reason_detail=f"Empresa explicitamente excluída ({excluded}).",
            )

    # 2. Localização centrada em Setúbal / Lisboa / Remoto PT
    geo_zone, geo_note = classify_geo(
        job.location,
        all_locations=job.all_locations,
        is_remote=job.is_remote,
        modality=job.modality,
        title=job.title,
        description=job.description or "",
        source_type=job.source_type,
        include_all_portugal=include_all_portugal,
    )
    if geo_zone == GeoZone.REJECTED_GEO:
        return FilterDecision(
            accepted=False,
            reason_code="ineligible_location",
            reason_detail=geo_note,
            geo_zone=geo_zone,
        )

    # 3. Janela temporal
    if recency_days is None:
        max_days = 14 if job.source_type == "ats" else (7 if job.source_type == "linkedin" else 10)
    else:
        max_days = recency_days

    is_recent, date_reason = recency_check(job.published_at, max_days=max_days, now=now)
    if not is_recent:
        return FilterDecision(
            accepted=False,
            reason_code=date_reason,
            reason_detail=f"Data de publicação rejeitada ({date_reason}, janela={max_days}d).",
            geo_zone=geo_zone,
        )

    # 4. Excluir funções não-técnicas no título
    non_tech_hit = _match_any(title_ascii, NON_TECH_PATTERNS)
    if non_tech_hit and not _allow_tech_override(title_ascii, non_tech_hit):
        return FilterDecision(
            accepted=False,
            reason_code="non_tech_role",
            reason_detail=f"Cargo fora de Informática/IA ({non_tech_hit!r}).",
            geo_zone=geo_zone,
        )

    # 5. Excluir títulos claramente sénior / mid / lead
    senior_hit = _match_any(title_ascii, SENIOR_MID_PATTERNS)
    if senior_hit and not _allow_senior_override(title_ascii, senior_hit):
        return FilterDecision(
            accepted=False,
            reason_code="excluded_seniority_title",
            reason_detail=f"Título indica nível não-entry ({senior_hit!r}).",
            geo_zone=geo_zone,
        )

    senior_meta_hit = _match_any(seniority_ascii, SENIOR_MID_PATTERNS)
    if senior_meta_hit:
        return FilterDecision(
            accepted=False,
            reason_code="excluded_seniority_metadata",
            reason_detail=f"Senioridade da fonte indica nível não-entry ({senior_meta_hit!r}).",
            geo_zone=geo_zone,
        )

    # 6. Verificar sinal REAL de Estágio ou Entry-Level (SEM nunca usar discovery_query!)
    role_tier: RoleTier | None = None
    evidence_str = ""

    # 6a. Sinais explícitos de Estágio / Trainee / Graduate
    for field_name, value in (
        ("title", title_ascii),
        ("employment_type", emp_type_ascii),
        ("seniority", seniority_ascii),
        ("tags", tags_ascii),
    ):
        hit = _match_any(value, INTERNSHIP_PATTERNS)
        if hit:
            role_tier = RoleTier.INTERNSHIP
            evidence_str = f"{field_name}:{hit}"
            break

    # 6b. Se não for claramente estágio nos metadados principais, verifica descrição só para estágio explícito
    if role_tier is None and desc_ascii:
        estagio_desc_hit = _match_any(
            desc_ascii,
            [
                r"\bestagio\s+(?:curricular|profissional|iefp|remunerado|extraccurricular|extracurricular|academico)\b",
                r"\b(?:curricular|professional|summer|paid)\s+internship\b",
                r"\bprogram(?:a|me)\s+de\s+estagios?\b",
                r"\binternship\s+program(?:me)?\b",
            ],
        )
        if estagio_desc_hit:
            role_tier = RoleTier.INTERNSHIP
            evidence_str = f"description:{estagio_desc_hit}"

    # 6c. Se ainda não for estágio, verifica Junior / Entry-level (0-1 ano)
    if role_tier is None:
        for field_name, value in (
            ("title", title_ascii),
            ("employment_type", emp_type_ascii),
            ("seniority", seniority_ascii),
        ):
            hit = _match_any(value, JUNIOR_PATTERNS)
            if hit:
                role_tier = RoleTier.ENTRY_JUNIOR
                evidence_str = f"{field_name}:{hit}"
                break

    if role_tier is None and desc_ascii:
        desc_entry = _match_any(
            desc_ascii,
            [
                r"\b0\s*(?:-|a|to)\s*[12]\s+(?:years?|anos?)\b",
                r"\bno\s+prior\s+experience\b",
                r"\bsem\s+experiencia\s+(?:previa|profissional)\b",
                r"\brecem[- ]licenciad[oa]\b",
            ],
        )
        if desc_entry:
            role_tier = RoleTier.ENTRY_JUNIOR
            evidence_str = f"description:{desc_entry}"

    if role_tier is None:
        return FilterDecision(
            accepted=False,
            reason_code="not_internship_or_entry_level",
            reason_detail="Sem sinal explícito de estágio, trainee ou entry-level no anúncio real.",
            geo_zone=geo_zone,
        )

    # 7. Verificar se a descrição exige 2+/3+ anos de experiência (falso Junior/Intern)
    if desc_ascii:
        exp_match = DESC_DISQUALIFYING_EXPERIENCE_RE.search(desc_ascii)
        if exp_match and role_tier == RoleTier.ENTRY_JUNIOR:
            return FilterDecision(
                accepted=False,
                reason_code="requires_mid_senior_experience",
                reason_detail=f"Descrição exige experiência incompatível ({exp_match.group(0)!r}).",
                geo_zone=geo_zone,
            )

    # 8. Classificar domínio técnico (Informática / IA / Dados / SWE / Cyber / Cloud / Embedded)
    domain = classify_domain(title_ascii, tags_ascii)
    if domain is None and role_tier == RoleTier.INTERNSHIP and desc_ascii:
        # Só aceita título genérico ("Estágio Académico", "Trainee Program") se a descrição for claramente Informática/IA
        if _match_any(
            desc_ascii,
            [
                r"\bengenharia\s+informatica\b",
                r"\bcomputer\s+science\b",
                r"\bsoftware\s+(?:development|engineering)\b",
                r"\bmachine\s+learning\b",
                r"\bartificial\s+intelligence\b",
                r"\binteligencia\s+artificial\b",
                r"\bdeep\s+learning\b",
                r"\bnlp\b",
                r"\bcomputer\s+vision\b",
                r"\bvisao\s+computacional\b",
                r"\bdata\s+science\b",
                r"\bpython\b",
                r"\bcybersecurity\b",
                r"\brobotics\b",
            ],
        ):
            domain = DomainCategory.AI_ML if _match_any(desc_ascii, AI_ML_PATTERNS) else DomainCategory.SWE

    if domain is None and role_tier == RoleTier.INTERNSHIP and extra_tech_company_override:
        domain = DomainCategory.SWE

    if domain is None:
        return FilterDecision(
            accepted=False,
            reason_code="non_tech_domain",
            reason_detail="Título/tags não correspondem a Informática, IA, Dados ou Engenharia de Software.",
            geo_zone=geo_zone,
        )

    return FilterDecision(
        accepted=True,
        reason_code="accepted",
        reason_detail=f"Aceite ({role_tier.value}, {domain.value}, {geo_zone.value}) via {evidence_str}.",
        role_tier=role_tier,
        domain=domain,
        geo_zone=geo_zone,
    )
