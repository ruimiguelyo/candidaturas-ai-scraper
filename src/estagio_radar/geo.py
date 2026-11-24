"""Classificador geográfico centrado em Setúbal -> Grande Lisboa -> Remoto PT."""

from __future__ import annotations

import re
import unicodedata

from estagio_radar.models import GeoZone

SETUBAL_PATTERNS = [
    r"\bsetubal\b",
    r"\bpalmela\b",
    r"\bquinta do anjo\b",
    r"\bpinhal novo\b",
    r"\bmoita\b",
    r"\bbarreiro\b",
    r"\bmontijo\b",
    r"\balcochete\b",
    r"\bseixal\b",
    r"\balmada\b",
    r"\bcaparica\b",
    r"\bcosta da caparica\b",
    r"\bmonte d[ae] caparica\b",
    r"\bfct[- ]nova\b",
    r"\binstituto politecnico de setubal\b",
    r"\bsesimbra\b",
    r"\bazeitao\b",
    r"\bcorroios\b",
    r"\bcoina\b",
    r"\bmargem sul\b",
    r"\bpeninsula de setubal\b",
    r"\bautoeuropa\b",
]

LISBOA_PATTERNS = [
    r"\blisboa\b",
    r"\blisbon\b",
    r"\blisbon metropolitan area\b",
    r"\bgrande lisboa\b",
    r"\boeiras\b",
    r"\btaguspark\b",
    r"\blagoas park\b",
    r"\bquinta da fonte\b",
    r"\bcarnaxide\b",
    r"\blinda[- ]a[- ]velha\b",
    r"\balges\b",
    r"\bpaco de arcos\b",
    r"\bmiraflores\b",
    r"\bcascais\b",
    r"\bestoril\b",
    r"\bsintra\b",
    r"\balbarraque\b",
    r"\bamadora\b",
    r"\balfragide\b",
    r"\bloures\b",
    r"\bsacavem\b",
    r"\bprior velho\b",
    r"\bmoscavide\b",
    r"\bparque das nacoes\b",
    r"\bodivelas\b",
    r"\bvila franca de xira\b",
    r"\balverca\b",
    r"\bporto salvo\b",
    r"\bbarcarena\b",
    r"\bist-id\b",
    r"\binesc-id\b",
    r"\binesc\b",
    r"\bfcul\b",
    r"\biscte\b",
    r"\bchampalimaud\b",
    r"\bgulbenkian\b",
    r"\binstituto superior tecnico\b",
]

OTHER_PT_CITY_PATTERNS = [
    r"\bporto\b",
    r"\bmaia\b",
    r"\bmatosinhos\b",
    r"\bgaia\b",
    r"\bvila nova de gaia\b",
    r"\bbraga\b",
    r"\bguimaraes\b",
    r"\baveiro\b",
    r"\bcoimbra\b",
    r"\bleiria\b",
    r"\bviseu\b",
    r"\bevora\b",
    r"\bfaro\b",
    r"\balgarve\b",
    r"\bmadeira\b",
    r"\bfunchal\b",
    r"\bacores\b",
    r"\bazores\b",
    r"\bcastelo branco\b",
    r"\bsantarem\b",
    r"\bguarda\b",
    r"\bportalegre\b",
    r"\bvila real\b",
    r"\bviana do castelo\b",
    r"\blousado\b",
    r"\bovar\b",
    r"\bfafe\b",
    r"\bfundao\b",
    r"\bcovilha\b",
    r"\bbeja\b",
    r"\bbraganca\b",
    r"\bcaldas da rainha\b",
    r"\bponte de sor\b",
    r"\btorres vedras\b",
    r"\btomar\b",
    r"\babrantes\b",
    r"\bfigueira da foz\b",
    r"\bchaves\b",
    r"\bbarcelos\b",
    r"\bfamalicao\b",
]

REMOTE_KEYWORDS = [
    r"\bremote\b",
    r"\bremot[oa]\b",
    r"\bteletrabalho\b",
    r"\bhome[- ]based\b",
    r"\bwork from anywhere\b",
    r"\bdistributed\b",
]

PT_OR_ELIGIBLE_REGION_PATTERNS = [
    r"\bportugal\b",
    r"\beurope\b",
    r"\beuropean union\b",
    r"\bemea\b",
    r"\beu\b",
    r"\bworldwide\b",
    r"\bglobal\b",
    r"\banywhere\b",
    r"\bcet\b",
    r"\bwet\b",
    r"\bgmt\b",
]

# Países e códigos que tornam uma vaga remota INCOMPATÍVEL se Portugal/Europe/Worldwide não estiver presente
FOREIGN_COUNTRY_PATTERNS = [
    r"\bunited states\b",
    r"\bu\.s\.a?\b",
    r"\busa\b",
    r"\bcanada\b",
    r"\bunited kingdom\b",
    r"\buk\b",
    r"\bengland\b",
    r"\bscotland\b",
    r"\bireland\b",
    r"\bindia\b",
    r"\bbrazil\b",
    r"\bbrasil\b",
    r"\baustralia\b",
    r"\bnew zealand\b",
    r"\bgermany\b",
    r"\bdeutschland\b",
    r"\bfrance\b",
    r"\bspain\b",
    r"\bmadrid\b",
    r"\bbarcelona\b",
    r"\bitaly\b",
    r"\bpoland\b",
    r"\bphilippines\b",
    r"\bcolombia\b",
    r"\bmexico\b",
    r"\bargentina\b",
    r"\bsri lanka\b",
    r"\bsingapore\b",
    r"\bjapan\b",
    r"\bchina\b",
    r"\bswitzerland\b",
    r"\baustria\b",
    r"\bnetherlands\b",
    r"\bbelgium\b",
    r"\bsweden\b",
    r"\bnorway\b",
    r"\bdenmark\b",
    r"\bfinland\b",
    r"\bromania\b",
    r"\bczech\b",
    r"\bhungary\b",
    r"\bbulgaria\b",
    r"\bturkey\b",
    r"\bisrael\b",
    r"\buae\b",
    r"\bdubai\b",
    r"\bpakistan\b",
    r"\bvietnam\b",
    r"\bindonesia\b",
    r"\bmalaysia\b",
    r"\bnigeria\b",
    r"\bsouth africa\b",
    r"\blatam\b",
    r"\bamericas\b",
    r"\bnorth america\b",
    r"\bapac\b",
    # Cidades / estados dos EUA comuns em títulos/localizações
    r"\b(?:san francisco|new york|austin|seattle|boston|chicago|norman|california|texas)\b",
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b",
]

US_ONLY_WORK_AUTH_PATTERNS = [
    r"authorized to work in the (?:united states|u\.s\.|us)\b",
    r"us citizenship",
    r"must reside in the (?:united states|u\.s\.|us)\b",
    r"must be located in the (?:united states|u\.s\.|us)\b",
    r"work authorization in the (?:united states|us)\b",
    r"remote[- ]?(?:only )?(?:within|in) the (?:us|united states)\b",
]

# Padrões case-sensitive para códigos curtos como "US" / "PT" / estados EUA
STRICT_CASE_FOREIGN_RE = re.compile(r"\b(?:US|USA|UK|CA|AU|IN|PH|CO|BR|DE|FR|NL|PL|ES)\b")


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _any_match(text: str, patterns: list[str]) -> bool:
    return any(re.search(pat, text, re.IGNORECASE) for pat in patterns)


def classify_geo(
    location: str,
    *,
    all_locations: list[str] | None = None,
    is_remote: bool = False,
    modality: str = "",
    title: str = "",
    description: str = "",
    source_type: str = "board",
    include_all_portugal: bool = False,
) -> tuple[GeoZone, str]:
    """Classifica compatibilidade geográfica centrada em Setúbal e Lisboa."""
    loc_parts = [location or ""]
    if all_locations:
        loc_parts.extend(all_locations)
    raw_combined_locs = " | ".join(p for p in loc_parts if p).strip()
    loc_norm = _norm(raw_combined_locs)
    title_norm = _norm(title)
    modality_norm = _norm(modality)
    desc_norm = _norm(description or "")

    # Verificação de autorização estrita nos EUA na descrição
    if desc_norm and _any_match(desc_norm, US_ONLY_WORK_AUTH_PATTERNS):
        if "portugal" not in loc_norm and "lisbon" not in loc_norm and "lisboa" not in loc_norm:
            return GeoZone.REJECTED_GEO, "Restrito a autorização de trabalho nos EUA."

    # Avalia título + localizações combinadas para casos tipo "Software Intern - Lisbon, PT"
    geo_text = f"{loc_norm} {title_norm}"

    # 1. Setúbal / Palmela / Margem Sul (prioridade máxima para o utilizador!)
    if _any_match(geo_text, SETUBAL_PATTERNS):
        return GeoZone.SETUBAL_LOCAL, "Localização na Península de Setúbal / Margem Sul."

    # 2. Lisboa & Grande Lisboa
    if _any_match(geo_text, LISBOA_PATTERNS):
        return GeoZone.LISBOA_METRO, "Localização na Área Metropolitana de Lisboa."

    # 3. Verificar se menciona explicitamente Portugal (sem cidade específica ou com outra cidade)
    mentions_portugal = bool(re.search(r"\bportugal\b", geo_text)) or bool(
        re.search(r",\s*PT\b|\bPT\b", f"{raw_combined_locs} {title}")
    )
    mentions_other_pt_city = _any_match(loc_norm, OTHER_PT_CITY_PATTERNS)

    # Verifica sinais remotos
    remote_signal = bool(
        is_remote
        or _any_match(loc_norm, REMOTE_KEYWORDS)
        or _any_match(modality_norm, REMOTE_KEYWORDS)
        or _any_match(title_norm, REMOTE_KEYWORDS)
    )

    # Se for outra cidade portuguesa (ex: Porto/Braga/Aveiro) SEM Lisboa/Setúbal e SEM remoto: rejeitar salvo --all-portugal!
    if mentions_other_pt_city and not remote_signal:
        if include_all_portugal:
            return GeoZone.PT_OTHER_CITY, "Outra cidade portuguesa (incluída via --all-portugal)."
        return GeoZone.REJECTED_GEO, "Presencial/híbrido noutra cidade portuguesa (fora de Setúbal/Lisboa)."

    # Se menciona explicitamente país/região estrangeira no título ou localização sem mencionar Portugal/Europe/Worldwide
    has_foreign = (
        _any_match(loc_norm, FOREIGN_COUNTRY_PATTERNS)
        or _any_match(title_norm, FOREIGN_COUNTRY_PATTERNS)
        or bool(STRICT_CASE_FOREIGN_RE.search(f"{raw_combined_locs} {title}"))
    )
    mentions_eu_or_world = _any_match(loc_norm, PT_OR_ELIGIBLE_REGION_PATTERNS) or _any_match(
        title_norm, PT_OR_ELIGIBLE_REGION_PATTERNS
    )

    if remote_signal:
        if mentions_portugal:
            return GeoZone.REMOTE_PT, "Remoto com elegibilidade confirmada em Portugal."
        if has_foreign and not (mentions_eu_or_world and "worldwide" in f"{loc_norm} {title_norm}"):
            # Ex: "Remote - US", "Remote - United States", "Remote - India", "San Francisco, CA; Remote, US"
            # Mesmo que diga "EMEA" junto com UK-only ou US, se tiver foreign específico sem Europe/Worldwide, rejeita!
            if not re.search(r"\b(?:europe|european union|emea|worldwide|global|anywhere)\b", loc_norm):
                return GeoZone.REJECTED_GEO, "Remoto restrito a país/região estrangeira."
            # Se diz "United Kingdom" ou "United States" junto com EMEA/Europe, rejeita se for país único explícito
            if re.search(r"\b(?:united states|usa|india|canada|australia|philippines|brazil)\b", geo_text):
                return GeoZone.REJECTED_GEO, "Remoto com restrição geográfica incompatível."
        if mentions_eu_or_world:
            return GeoZone.REMOTE_PT, "Remoto elegível na Europa / EMEA / Global."
        # "Remote" genérico sem país: só aceitar se vier de fontes portuguesas/europeias
        if source_type == "pt_board":
            return GeoZone.REMOTE_PT, "Remoto em portal português."
        return GeoZone.REJECTED_GEO, "Remoto sem confirmação de elegibilidade para Portugal/Europa."

    if mentions_portugal and not mentions_other_pt_city:
        return GeoZone.PT_UNSPECIFIED, "Localização em Portugal (cidade não especificada)."

    if has_foreign:
        return GeoZone.REJECTED_GEO, "Localização fora de Portugal."

    if source_type == "pt_board" and loc_norm in {"", "unknown", "n/a", "portugal"}:
        return GeoZone.PT_UNSPECIFIED, "Localização genérica em portal português."

    return GeoZone.REJECTED_GEO, f"Localização incompatível ou não verificada ({location})."
