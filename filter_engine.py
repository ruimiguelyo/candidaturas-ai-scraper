import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from date_utils import RECENCY_WINDOW_DAYS, recency_status
from models import JobPost


@dataclass(frozen=True)
class FilterDecision:
    """Resultado auditável do pré-filtro de uma vaga."""

    accepted: bool
    reason_code: str
    reason_detail: str
    job: Optional[JobPost] = None
    domain_type: Optional[str] = None


class JobFilterEngine:
    """Aplica uma política auditável de relevância sem excluir por rating ou localização."""

    AI_KEYWORDS = [
        r"\bai\b",
        r"\bml\b",
        r"\bnlp\b",
        r"\brag\b",
        r"\bllms?\b",
        r"\bartificial intelligence\b",
        r"\bintelig[eê]ncia artificial\b",
        r"\bmachine learning\b",
        r"\bdeep learning\b",
        r"\blarge language models?\b",
        r"\bgenerative ai\b",
        r"\bgen[- ]?ai\b",
        r"\bnatural language processing\b",
        r"\bcomputer vision\b",
        r"\bvis[aã]o computacional\b",
        r"\bdata science\b",
        r"\bdata scientist\b",
        r"\bdata engineer(?:ing)?\b",
        r"\bdata analyst\b",
        r"\bdata (?:analysis|analytics|governance|integrity|insights|management|services|strategy)\b",
        r"\bdata\s*(?:&|and)\s*(?:insights|reporting|research)\b",
        r"\bdata intern\b",
        r"\breporting analyst\b",
        r"\banalytics\b",
        r"\banalista de dados\b",
        r"\ban[aá]lise de dados\b",
        r"\bbusiness intelligence\b",
        r"\binvestiga[cç][aã]o operacional\b",
        r"\bci[eê]ncia de dados\b",
        r"\bcientista de dados\b",
        r"\bengenheiro de dados\b",
        r"\btransformers?\b",
        r"\bprompt engineer(?:ing)?\b",
        r"\bmlops\b",
        r"\bai engineer(?:ing)?\b",
        r"\bia\b",
        r"\bresearch scientist\b",
        r"\binvestigador\b",
        r"\btech\s*&\s*ai\b",
    ]

    SOFTWARE_ENG_KEYWORDS = [
        r"\bsoftware engineer(?:ing)?\b",
        r"\bengenheiro de software\b",
        r"\bsoftware developer\b",
        r"\bsoftware dev\b",
        r"\bsw developer\b",
        r"\bdeveloper\b",
        r"\bdesenvolvedor(?:a)?\b",
        r"\bdesenvolvedor de software\b",
        r"\bsystems engineer(?:ing)?\b",
        r"\bengenheiro de sistemas\b",
        r"\bsystems? analyst\b",
        r"\banalista de sistemas\b",
        r"\bnetwork engineer(?:ing)?\b",
        r"\bengenheir[oa] (?:de )?redes?\b",
        r"\bbackend\b",
        r"\bback[- ]end\b",
        r"\bfull[- ]?stack\b",
        r"\bcloud engineer(?:ing)?\b",
        r"\bdevops\b",
        r"\binfrastructure\b",
        r"\bqa engineer(?:ing)?\b",
        r"\bqa\b",
        r"\bquality assurance\b",
        r"\bsoftware quality\b",
        r"\b(?:software |browser )?tester\b",
        r"\btesting\b",
        r"\bsoftware development\b",
        r"\bprogramador(?:a)?\b",
        r"\bfront[- ]?end\b",
        r"\bmobile\b",
        r"\bios\b",
        r"\bandroid\b",
        r"\bcyber[- ]?security\b",
        r"\bciber[- ]?seguran[cç]a\b",
        r"\bsecurity\b",
        r"\bseguran[cç]a (?:da informa[cç][aã]o|inform[aá]tica)\b",
        r"\binformation security\b",
        r"\bappsec\b",
        r"\bdevsecops\b",
        r"\bcyber risk\b",
        r"\bphishing\b",
        r"\bapplication security\b",
        r"\bcloud security\b",
        r"\bsecurity engineer(?:ing)?\b",
        r"\bsecurity analyst\b",
        r"\bpython\b",
        r"\b(?:c\+\+|c#|\.net|php|java|javascript|typescript|react|angular|golang|rust)\b",
        r"\bcomputer sciences?\b",
        r"\binformation technology\b",
        r"\btecnologias? (?:da|de) informa[cç][aã]o\b",
        r"\bit engineer(?:ing)?\b",
        r"\b(?:application|product|technical|it) support\b",
        r"\b(?:erp|sap|salesforce|business central|mainframe)\b",
        r"\brpa\b",
        r"\biam\b",
        r"\b(?:ii?ot|industry 4\.0)\b",
        r"\b(?:analog|mixed[- ]signal|ic design)\b",
        r"\bresearch engineer(?:ing)?\b",
        r"\bperformance engineer(?:ing)?\b",
        r"\bquality engineer(?:ing)?\b",
        r"\bsolutions? architect\b",
        r"\btechnical consultant\b",
        r"\bconsultor(?:a)? t[eé]cnic[oa]\b",
        r"\btechnology graduate\b",
        r"\bsystems? operations\b",
        r"\bplatform\b",
        r"\bplatform engineer(?:ing)?\b",
        r"\bsre\b",
        r"\bsite reliability\b",
        r"\bembedded\b",
        r"\bproduct engineer(?:ing)?\b",
        r"\bweb (?:developer|development|engineer|engineering)\b",
        r"\b(?:desenvolvimento|engenharia) web\b",
        r"\b(?:programador|engenheir)[oa]? web\b",
        r"\bapplication (?:developer|development|engineer|engineering)\b",
        (
            r"\b(?:desenvolvimento|engenharia|engenheir[oa]|desenvolvedor(?:a)?)\s+(?:de\s+)?"
            r"aplica[cç](?:[aã]o|[oõ]es)\b"
        ),
        r"\btest (?:developer|engineer|engineering|automation)\b",
        r"\bautomation (?:developer|engineer|engineering)\b",
        r"\b(?:qa|test) automation\b",
        r"\b(?:engenharia|engenheir[oa]) (?:de )?testes\b",
        r"\b(?:automa[cç][aã]o de testes|testes automatizados)\b",
        r"\b(?:engenharia|engenheir[oa]) (?:de )?automa[cç][aã]o\b",
    ]

    # Estes são os sinais explícitos que podem demonstrar nível entry-level no
    # título, no campo de senioridade ou na descrição curta.
    STRICT_TITLE_KEYWORDS = [
        r"\bjunior\b",
        r"\bj[uú]nior\b",
        r"\bjr\.?\b",
        r"\btrainee\b",
        r"\bintern(?:ship)?\b",
        r"\best[aá]gio\b",
        r"\bestagi[aá]rio\b",
        r"\bgraduate\b",
        r"\bentry[- ]level\b",
        r"\bentry[_ -]level\b",
        r"\bearly[- ]career\b",
        r"\bstudent\b",
        r"\bbolseir[oa]\b",
        r"\bbolsa\s+de\s+investiga[cç][aã]o\b",
        r"\bassociate\b",
        r"\bnew[- ]grad(?:uate)?\b",
        r"\bapprentic(?:e|eship)\b",
        r"\bacademy\b",
        r"\blevel\s*(?:i|1)\b",
        r"\b(?:engineer|developer)\s+i\b",
        r"\b0\s*(?:-|–|—|to)\s*2\s+years?\b",
        r"\b(?:0|1)\s*(?:-|–|—|to)\s*3\s+years?\b",
        r"\b(?:0|1)\s*(?:-|–|—|to)\s*2\s+years?\b",
        r"\bup\s+to\s+3\s+years?\b",
        r"\b(?:0|1|2|3)\+?\s+years?\s+(?:of\s+)?experience\b",
        r"\bno\s+(?:prior\s+)?experience\b",
        r"\bsem\s+experi[eê]ncia\b",
        r"\bprimeir[oa]\s+experi[eê]ncia\b",
        r"\bfirst\s+(?:professional\s+)?experience\b",
        r"\brec[eé]m[- ]licenciad[oa]\b",
    ]

    # Exclusões de senioridade são aplicadas ao título e ganham sobre qualquer
    # sinal entry-level que apareça nos restantes campos.
    SENIOR_EXCLUDE_KEYWORDS = [
        r"\bsenior\b",
        r"\bs[eé]nior\b",
        r"\bsr\.?\b",
        r"\blead\b",
        r"\bprincipal\b",
        r"\bstaff\b",
        r"\bmid[- ]level\b",
        r"\bmidweight\b",
        r"\bmid\b",
        r"\bpleno\b",
        r"\bjr\s*/\s*pl\b",
        r"\bjr\s*/\s*mid\b",
        r"\bdirector\b",
        r"\bhead\s+of\b",
        r"\b(?:engineer|developer|technician)\s+(?:ii|iii|iv)\b",
    ]

    # Funções inequivocamente não técnicas. Termos ambíguos como operations,
    # content, manager e gerente não são bloqueios isolados.
    NON_TECHNICAL_EXCLUDE_KEYWORDS = [
        r"\baccount executive\b",
        r"\bbusiness analyst\b",
        r"\bbusiness development\b",
        r"\bbusiness developer\b",
        r"\breal estate developer\b",
        r"\bcommercial (?:associate|coordinator|executive|intern|officer|representative|specialist)\b",
        (
            r"\bcommunications? "
            r"(?:associate|coordinator|consultant|intern|manager|officer|specialist)\b"
        ),
        r"\b(?:corporate|external|internal|marketing) communications?\b",
        r"\bcontent (?:creator|marketing|strategist|writer)\b",
        r"\bcounsel\b",
        r"\bgo[- ]to[- ]market\b",
        r"\bgtm\b",
        r"\bhuman resources\b",
        r"\bhr (?:management|manager|recruitment|recruiter)\b",
        r"\b(?:talent acquisition|recruitment|recruiter)\b",
        r"\blearning (?:and|&) development manager\b",
        r"\blegal\b",
        r"\bmarketing\b",
        r"\b(?:business|career|marketing|sales) mentor\b",
        r"\bsales\b",
        r"\b(?:business|commercial|customer success|partnerships?|people|hr) operations\b",
        (
            r"\b(?:program(?:me)?|project|product|sales|marketing|account|business|customer success|"
            r"partnerships?|operations|people|hr|commercial) manager\b"
        ),
        r"\b(?:gestor|gerente) (?:de )?(?:programa|projeto|produto|vendas|marketing|conta)s?\b",
        r"\bai\s*(?:&|and)\s*operations\b",
    ]

    # Alias legado para integrações que consultavam a lista diretamente.
    EXCLUDE_KEYWORDS = SENIOR_EXCLUDE_KEYWORDS + NON_TECHNICAL_EXCLUDE_KEYWORDS

    @staticmethod
    def _matches_any(text: str, patterns: list) -> bool:
        text = str(text or "")
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _matched_text(text: str, patterns: list) -> Optional[str]:
        text = str(text or "")
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    @classmethod
    def is_ai_related(cls, text: str) -> bool:
        return cls._matches_any(text, cls.AI_KEYWORDS)

    @classmethod
    def is_software_eng_related(cls, text: str) -> bool:
        return cls._matches_any(text, cls.SOFTWARE_ENG_KEYWORDS)

    @classmethod
    def has_strictly_entry_level_title(cls, title: str) -> bool:
        return cls._matches_any(title, cls.STRICT_TITLE_KEYWORDS)

    @classmethod
    def is_excluded(cls, title: str, company: str) -> bool:
        title_text = str(title or "")
        company_text = str(company or "")
        if re.search(r"deloitte", company_text, re.IGNORECASE) or re.search(
            r"deloitte", title_text, re.IGNORECASE
        ):
            return True
        return cls._matches_any(title_text, cls.EXCLUDE_KEYWORDS)

    @classmethod
    def annotate_location_compatibility(cls, job: JobPost) -> None:
        """Describe likely compatibility from Portugal without inventing work authorization."""
        location = str(job.location or "").casefold()
        if cls._matches_any(
            location,
            [
                r"\bportugal\b",
                r"\blisbo[an]\b",
                r"\bporto\b",
                r"\bcoimbra\b",
                r"\bbraga\b",
                r"\baveiro\b",
                r"\bfaro\b",
                r"\bset[uú]bal\b",
                r"\bleiria\b",
                r"\bviseu\b",
                r"\b[eé]vora\b",
                r"\bfunchal\b",
                r"\bmadeira\b",
                r"\ba[cç]ores\b",
                r"\bazores\b",
            ],
        ):
            job.location_compatibility = "confirmed"
            job.location_notes = ["A localização anunciada inclui Portugal."]
        elif cls._matches_any(location, [r"\bworldwide\b", r"\bglobal\b", r"\banywhere\b"]):
            job.location_compatibility = "conditional"
            job.location_notes = ["Remoto global; confirmar contrato, fuso horário e autorização de trabalho."]
        elif cls._matches_any(
            location,
            [
                r"\beurope\b",
                r"\beuropean union\b",
                r"\bemea\b",
                r"\beu\b",
                r"\baustria\b",
                r"\bbelgium\b",
                r"\bbulgaria\b",
                r"\bcroatia\b",
                r"\bcyprus\b",
                r"\bczech(?:ia| republic)?\b",
                r"\bdenmark\b",
                r"\bestonia\b",
                r"\bfinland\b",
                r"\bfrance\b",
                r"\bgermany\b",
                r"\bgreece\b",
                r"\bhungary\b",
                r"\biceland\b",
                r"\bireland\b",
                r"\bitaly\b",
                r"\blatvia\b",
                r"\bliechtenstein\b",
                r"\blithuania\b",
                r"\bluxembourg\b",
                r"\bmalta\b",
                r"\bnetherlands\b",
                r"\bnorway\b",
                r"\bpoland\b",
                r"\bromania\b",
                r"\bslovakia\b",
                r"\bslovenia\b",
                r"\bspain\b",
                r"\bsweden\b",
                r"\bswitzerland\b",
                r"\bunited kingdom\b",
            ],
        ):
            job.location_compatibility = "conditional"
            job.location_notes = ["A região anunciada inclui potencialmente Portugal; confirmar países aceites."]
        elif job.is_remote and cls._matches_any(location, [r"\bremote\b", r"\bremot[oa]\b"]):
            job.location_compatibility = "conditional"
            job.location_notes = ["Remoto, mas sem países elegíveis confirmados."]
        elif location and location not in {"unknown", "n/a", "na"}:
            job.location_compatibility = "unlikely"
            job.location_notes = ["A localização parece restrita fora da Europa; verificar antes de investir tempo."]
        else:
            job.location_compatibility = "unknown"
            job.location_notes = ["Compatibilidade geográfica não confirmada."]

    @classmethod
    def _classify_title(cls, title: str, tags: list) -> Optional[str]:
        """Classifica o cargo sem deixar uma tag promocional alterar um título SWE explícito."""
        title_is_ai = cls.is_ai_related(title)
        title_is_swe = cls.is_software_eng_related(title)

        # Um titulo explicitamente SWE continua a ser SWE se apenas a descricao
        # ou tags mencionarem AI de forma incidental. AI Software Engineer e o
        # caso intencionalmente hibrido e entra como AI.
        if title_is_ai:
            return "AI"
        if title_is_swe:
            return "SWE"

        tag_text = " ".join(str(tag) for tag in (tags or []))
        if cls.is_ai_related(tag_text):
            return "AI"
        if cls.is_software_eng_related(tag_text):
            return "SWE"
        return None

    @classmethod
    def _entry_level_evidence(cls, job: JobPost) -> Optional[Tuple[str, str]]:
        fields = (
            ("title", job.title),
            ("seniority", job.seniority),
            ("description_snippet", job.description_snippet),
            ("discovery_query", job.discovery_query),
        )
        for field_name, value in fields:
            patterns = cls.STRICT_TITLE_KEYWORDS
            if field_name == "description_snippet":
                # "Associate degree" is an education requirement, not evidence
                # that the role itself is associate/entry level.
                patterns = [pattern for pattern in patterns if pattern != r"\bassociate\b"]
            matched = cls._matched_text(str(value or ""), patterns)
            if matched:
                return field_name, matched
        return None

    @classmethod
    def evaluate_job(cls, job: JobPost, now: datetime | None = None) -> FilterDecision:
        """Decide o pré-filtro e explica de forma estruturada qualquer rejeição."""
        title_text = str(job.title or "").strip()
        company_text = str(job.company or "").strip()

        if re.search(r"deloitte", company_text, re.IGNORECASE) or re.search(
            r"deloitte", title_text, re.IGNORECASE
        ):
            return FilterDecision(
                accepted=False,
                reason_code="excluded_company",
                reason_detail="Deloitte está explicitamente excluída desta pesquisa.",
            )

        is_recent, published, recency_reason = recency_status(job.post_date, now=now)
        if not is_recent:
            if recency_reason == "outside_recency_window" and published is not None:
                detail = (
                    f"A vaga foi publicada em {published.date().isoformat()}, fora da janela "
                    f"dos últimos {RECENCY_WINDOW_DAYS} dias."
                )
            elif recency_reason == "invalid_post_date":
                detail = "A data de publicação está no futuro ou é inválida."
            else:
                detail = "A vaga não tem uma data de publicação verificável."
            return FilterDecision(
                accepted=False,
                reason_code=recency_reason,
                reason_detail=detail,
            )

        job.post_date = published.date().isoformat()

        title_exclusion = cls._matched_text(title_text, cls.EXCLUDE_KEYWORDS)
        if title_exclusion:
            is_senior_title = cls._matches_any(title_exclusion, cls.SENIOR_EXCLUDE_KEYWORDS)
            return FilterDecision(
                accepted=False,
                reason_code=("excluded_seniority" if is_senior_title else "excluded_non_technical"),
                reason_detail=(
                    f"O título contém um nível claramente sénior: {title_exclusion!r}."
                    if is_senior_title
                    else f"O título identifica uma função não técnica: {title_exclusion!r}."
                ),
            )

        seniority_match = cls._matched_text(str(job.seniority or ""), cls.SENIOR_EXCLUDE_KEYWORDS)
        if seniority_match:
            return FilterDecision(
                accepted=False,
                reason_code="excluded_seniority",
                reason_detail=(
                    "A metadata de senioridade contém um nível claramente sénior: "
                    f"{seniority_match!r}."
                ),
            )

        entry_evidence = cls._entry_level_evidence(job)
        if not entry_evidence:
            return FilterDecision(
                accepted=False,
                reason_code="missing_entry_level_signal",
                reason_detail=(
                    "Não foi encontrado um sinal entry-level explícito no título, "
                    "na senioridade ou na descrição."
                ),
            )

        domain_type = cls._classify_title(title_text, job.tags)
        if domain_type is None:
            return FilterDecision(
                accepted=False,
                reason_code="unsupported_domain",
                reason_detail="O título e as tags não identificam um domínio técnico suportado.",
            )

        if domain_type == "AI":
            job.category = "AI / ML"
        else:
            job.category = "Top-Tier Software Engineering"
        if not str(job.seniority or "").strip():
            job.seniority = entry_evidence[1]
        cls.annotate_location_compatibility(job)
        evidence_field, evidence_text = entry_evidence
        return FilterDecision(
            accepted=True,
            reason_code="accepted",
            reason_detail=(
                f"Domínio {domain_type} e sinal entry-level {evidence_text!r} "
                f"encontrado em {evidence_field}."
            ),
            job=job,
            domain_type=domain_type,
        )

    @classmethod
    def pre_filter_job(cls, job: JobPost) -> Optional[Tuple[JobPost, str]]:
        """API legada: mantém o tuple aceite/None e delega a explicação."""
        decision = cls.evaluate_job(job)
        if not decision.accepted or decision.job is None or decision.domain_type is None:
            return None
        return decision.job, decision.domain_type

    @staticmethod
    def is_eligible_after_rating(job: JobPost) -> bool:
        """Compatibilidade: rating e localização servem apenas para informação/ordenação."""
        return True
