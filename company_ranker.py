import asyncio
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

import bs4
import httpx


logger = logging.getLogger("CompanyRanker")

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_scores_cache.json")
ScoreResult = Dict[str, Any]


class TeamlyzerUnavailable(RuntimeError):
    """Indica que o Teamlyzer nao respondeu e nao que a empresa nao existe."""


class CompanyRanker:
    """
    Resolve a reputacao de uma empresa com a seguinte ordem:

    1. Autocomplete e perfis oficiais do Teamlyzer Portugal.
    2. Glassdoor apenas quando nao existe um perfil Teamlyzer valido.

    A cache mantem o formato simples historicamente usado pelo projeto. Entradas
    Glassdoor sao sempre revalidadas no Teamlyzer antes de serem reutilizadas, para
    que um perfil nacional criado posteriormente nunca fique escondido.
    """

    TEAMLYZER_BASE_URL = "https://pt.teamlyzer.com"
    TEAMLYZER_AUTOCOMPLETE_URL = f"{TEAMLYZER_BASE_URL}/users/autocomplete_company/v2/"
    GLASSDOOR_SEARCH_URL = "https://www.glassdoor.com/Search/results.htm?keyword={}"
    TEAMLYZER_RECHECK_SECONDS = 24 * 60 * 60

    _cache: Dict[str, Optional[ScoreResult]] = {}
    _cache_loaded = False

    # Aliases sao apenas uma rede de seguranca para entidades portuguesas conhecidas;
    # a descoberta dinamica continua a ser tentada primeiro.
    SPECIAL_PORTUGAL_ALIASES = {
        "philip morris": "tabaqueira",
        "philip morris international": "tabaqueira",
        "volkswagen": "volkswagen-digital-solutions",
        "volkswagen group": "volkswagen-digital-solutions",
        "santander": "santander-totta",
        "banco santander": "santander-totta",
        "innowave": "innowave-technologies",
        "innowave technologies": "innowave-technologies",
        "primeit": "prime-it",
        "prime it": "prime-it",
        "siemens": "siemens-sa",
        "bosch": "bosch-portugal",
        "bold": "bold-by-devoteam",
        "havi": "havi-portugal",
        "havi techhub": "havi-portugal",
        "mckinsey": "mckinsey-company",
        "mckinsey & company": "mckinsey-company",
        "mckinsey and company": "mckinsey-company",
        "nordea": "nordea-asset-management",
        "nordea asset management": "nordea-asset-management",
        "nordea asset management portugal": "nordea-asset-management",
    }

    SECONDARY_NAME_TOKENS = {
        "conclusion",
        "group",
        "services",
        "service",
        "solutions",
        "solution",
        "portugal",
        "portuguesa",
        "portugues",
        "gmbh",
        "inc",
        "llc",
        "ltd",
        "limited",
        "corporation",
        "corp",
        "operations",
        "sa",
        "s.a",
    }

    IGNORED_COMPANIES = {
        "",
        "n/a",
        "na",
        "confidencial",
        "consultar anuncio",
        "landing.jobs partner",
    }

    @staticmethod
    def _strip_accents(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", str(value))
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    @classmethod
    def _cache_key(cls, name: str) -> str:
        normalized = cls._strip_accents(name).lower()
        return re.sub(r"[^a-z0-9]", "", normalized)

    @classmethod
    def _normalize_name(cls, name: str) -> str:
        """Normaliza um nome para comparacao e pesquisa, sem o alterar para exibicao."""
        clean = cls._strip_accents(name).lower().strip()
        clean = clean.replace("&", " and ")
        clean = re.sub(r"\bs\.?\s*a\.?\b", " ", clean)
        clean = re.sub(r"[(),.;]+", " ", clean)
        tokens = re.findall(r"[a-z0-9]+", clean)
        while tokens and tokens[-1] in cls.SECONDARY_NAME_TOKENS:
            tokens.pop()
        return " ".join(tokens) or cls._strip_accents(name).strip().lower()

    @classmethod
    def _slugify(cls, name: str) -> str:
        clean = cls._strip_accents(name).lower().replace("&", " and ")
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", clean)).strip("-")

    @classmethod
    def _special_alias_slug(cls, company_name: str) -> Optional[str]:
        compact = " ".join(cls._strip_accents(company_name).lower().split())
        return cls.SPECIAL_PORTUGAL_ALIASES.get(compact)

    @classmethod
    def _teamlyzer_was_checked_recently(cls, cached: ScoreResult) -> bool:
        try:
            checked_at = float(cached.get("teamlyzer_checked_at", 0) or 0)
        except (TypeError, ValueError):
            return False
        return checked_at > 0 and time.time() - checked_at < cls.TEAMLYZER_RECHECK_SECONDS

    @classmethod
    def _trusted_teamlyzer_cache(cls, company_name: str, cached: ScoreResult) -> bool:
        """Rejeita resultados antigos que parecem perfis aleatorios sem reviews."""
        try:
            numeric = float(cached.get("numeric", 0) or 0)
        except (TypeError, ValueError):
            numeric = 0.0
        reviews = str(cached.get("reviews", ""))
        if numeric >= 5.0 and re.search(r"\b0\s+reviews?\b", reviews, re.IGNORECASE):
            return False

        matched_name = str(cached.get("matched_name", "")).strip()
        if matched_name and not cached.get("alias_match"):
            return cls._company_similarity(company_name, matched_name) >= 0.8
        return True

    @classmethod
    def _legacy_teamlyzer_cache_is_plausible(cls, company_name: str, cached: ScoreResult) -> bool:
        """Verifica rapidamente cache antiga sem fazer uma chamada de rede."""
        if not cls._trusted_teamlyzer_cache(company_name, cached):
            return False
        url = str(cached.get("url", ""))
        slug_match = re.search(r"/companies/([^/?#]+)", url)
        if not slug_match:
            return False
        slug = slug_match.group(1).lower()
        expected_tokens = set(cls._normalize_name(company_name).split())
        slug_tokens = set(re.findall(r"[a-z0-9]+", cls._strip_accents(slug).lower()))
        if expected_tokens and expected_tokens.issubset(slug_tokens):
            return True

        expected_compact = cls._cache_key(company_name)
        slug_compact = re.sub(r"[^a-z0-9]", "", cls._strip_accents(slug).lower())
        if expected_compact and (
            expected_compact == slug_compact
            or expected_compact in slug_compact
            or slug_compact in expected_compact
        ):
            return True

        special_slug = cls._special_alias_slug(company_name)
        return bool(special_slug and special_slug == slug)

    @classmethod
    def _name_variants(cls, company_name: str) -> List[str]:
        """Gera consultas exatas e consultas progressivamente menos especificas."""
        original = " ".join(str(company_name).split()).strip()
        normalized = cls._normalize_name(original)
        original_tokens = re.findall(r"[a-z0-9]+", cls._strip_accents(original).lower())
        normalized_tokens = normalized.split()

        variants: List[str] = []

        def add(value: str) -> None:
            value = " ".join(value.replace("&", " and ").split()).strip()
            if value and value not in variants:
                variants.append(value)

        add(original)
        add(original.replace("&", "and"))
        add(normalized)

        # Retira sufixos secundarios em qualquer posicao e nao apenas no fim.
        for index, token in enumerate(normalized_tokens):
            if token in cls.SECONDARY_NAME_TOKENS:
                add(" ".join(normalized_tokens[:index] + normalized_tokens[index + 1 :]))
        add(" ".join(token for token in normalized_tokens if token not in cls.SECONDARY_NAME_TOKENS))

        # Termos individuais ajudam nos casos em que o nome oficial contem mais de
        # uma entidade, por exemplo "Innovation & Engineering HUB".
        for token in original_tokens + normalized_tokens:
            if len(token) >= 3 and token not in cls.SECONDARY_NAME_TOKENS and token not in {"and", "the", "of"}:
                add(token)

        return variants

    @classmethod
    def load_cache(cls) -> None:
        if cls._cache_loaded:
            return

        cls._cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as cache_file:
                    loaded = json.load(cache_file)
                if isinstance(loaded, dict):
                    cls._cache = loaded
                else:
                    logger.warning("Cache de rankings ignorada: formato invalido")
            except (OSError, json.JSONDecodeError) as error:
                logger.warning("Erro ao carregar cache de rankings: %s", error)
        cls._cache_loaded = True

    @classmethod
    def save_cache(cls) -> None:
        """Persiste a cache com substituicao atomica para evitar JSON truncado."""
        directory = os.path.dirname(CACHE_FILE) or "."
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix="company_scores_cache.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                json.dump(cls._cache, temporary_file, indent=2, ensure_ascii=False)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, CACHE_FILE)
        except OSError as error:
            logger.warning("Erro ao salvar cache de rankings: %s", error)
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    @classmethod
    def _company_similarity(cls, expected: str, candidate: str) -> float:
        expected_normalized = cls._normalize_name(expected)
        candidate_normalized = cls._normalize_name(candidate)
        if not expected_normalized or not candidate_normalized:
            return 0.0
        if expected_normalized == candidate_normalized:
            return 1.0

        expected_tokens = set(expected_normalized.split())
        candidate_tokens = set(candidate_normalized.split())
        # Nao aceitar nomes apenas porque partilham uma palavra generica
        # ("Learning", "One", "Associates", etc.). O perfil tem de conter todos
        # os tokens significativos do nome procurado; sufixos secundários ja foram
        # removidos por _normalize_name.
        if expected_tokens.issubset(candidate_tokens):
            return 0.85
        return 0.0

    @classmethod
    def _is_teamlyzer_profile_url(cls, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(str(url))
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.netloc.lower() == "pt.teamlyzer.com"
            and bool(re.fullmatch(r"/companies/[^/?#]+/?", parsed.path))
        )

    @classmethod
    def _canonical_teamlyzer_url(cls, url: str, fallback: str) -> Optional[str]:
        candidate = str(url or fallback)
        try:
            parsed = urllib.parse.urlparse(candidate)
        except ValueError:
            return None
        if parsed.netloc.lower() != "pt.teamlyzer.com":
            return None
        path_match = re.fullmatch(r"/companies/([^/?#]+)/?", parsed.path)
        if not path_match:
            return None
        return f"{cls.TEAMLYZER_BASE_URL}/companies/{path_match.group(1)}"

    @classmethod
    def _response_json(cls, response: Any) -> Any:
        try:
            return response.json()
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            try:
                return json.loads(response.text)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return None

    @classmethod
    def _autocomplete_candidates(cls, payload: Any) -> Iterable[Tuple[str, str]]:
        if isinstance(payload, dict):
            items = payload.get("results") or payload.get("json_list") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        if isinstance(items, dict):
            items = items.values()

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("label") or item.get("title") or "").strip()
            slug = str(item.get("slug") or item.get("value") or "").strip()
            if "/companies/" in slug:
                slug = slug.split("/companies/", 1)[1].split("/", 1)[0]
            slug = cls._slugify(slug)
            if slug:
                yield name or slug.replace("-", " "), slug

    @classmethod
    def _parse_teamlyzer_profile(
        cls, html: str, response_url: str, expected_name: str, allow_alias: bool = False
    ) -> Optional[ScoreResult]:
        soup = bs4.BeautifulSoup(html or "", "html.parser")
        canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_href = canonical_tag.get("href", "") if canonical_tag else ""
        canonical_url = cls._canonical_teamlyzer_url(canonical_href, response_url)
        if not canonical_url:
            return None

        heading = soup.select_one("h1.reduce-h1") or soup.select_one(".company_header_basic_info h1")
        heading_name = heading.get_text(" ", strip=True) if heading else ""
        name_meta = soup.select_one('span[itemprop="itemReviewed"] meta[itemprop="name"]')
        profile_name = heading_name or (name_meta.get("content", "").strip() if name_meta else "")
        if not profile_name or re.search(r"erro\s*404", profile_name, re.IGNORECASE):
            return None

        similarity = cls._company_similarity(expected_name, profile_name)
        if similarity < 0.45 and not allow_alias:
            return None

        rating_meta = soup.select_one('span[itemprop="aggregateRating"] meta[itemprop="ratingValue"]')
        review_meta = soup.select_one('span[itemprop="aggregateRating"] meta[itemprop="reviewCount"]')
        rating_text = rating_meta.get("content", "").replace(",", ".") if rating_meta else ""
        score_match = re.search(r"([1-5](?:\.[0-9])?)", rating_text)

        if not score_match:
            visual_rating = (
                soup.select_one(".score-value-wrapper .c_rating")
                or soup.select_one(".score-value-wrapper")
                or soup.select_one(".c_rating")
                or soup.select_one(".b_rating")
            )
            visual_text = visual_rating.get_text(" ", strip=True) if visual_rating else ""
            score_match = re.search(r"([1-5](?:[.,][0-9])?)", visual_text)

        numeric = float(score_match.group(1).replace(",", ".")) if score_match else 0.0
        score = f"{numeric:.1f}/5" if score_match else "Ver Reviews"

        reviews = review_meta.get("content", "").strip() if review_meta else ""
        if reviews:
            reviews_text = f"{reviews} Reviews" if reviews.isdigit() else reviews
        else:
            visible_reviews = soup.select_one(".label_rating_font")
            reviews_text = visible_reviews.get_text(" ", strip=True) if visible_reviews else "Sem reviews"

        # Uma nota agregada com zero reviews e um artefacto comum de perfis
        # irrelevantes devolvidos pelo autocomplete. Nao publicar essa nota.
        if score_match and reviews_text.lower() in {"0 reviews", "0 review"}:
            return None

        return {
            "platform": "Teamlyzer",
            "score": score,
            "numeric": numeric,
            "reviews": reviews_text,
            "url": canonical_url,
            "matched_name": profile_name,
            "alias_match": allow_alias,
        }

    @classmethod
    async def _fetch_teamlyzer_profile(
        cls, client: httpx.AsyncClient, slug: str, expected_name: str, allow_alias: bool = False
    ) -> Optional[ScoreResult]:
        url = f"{cls.TEAMLYZER_BASE_URL}/companies/{slug}"
        try:
            response = await client.get(url, timeout=4.0, follow_redirects=True)
        except (httpx.RequestError, asyncio.TimeoutError) as error:
            raise TeamlyzerUnavailable(str(error)) from error

        status = int(getattr(response, "status_code", 0) or 0)
        if status in {403, 429} or status >= 500:
            raise TeamlyzerUnavailable(f"Teamlyzer respondeu HTTP {status}")
        if status != 200:
            return None

        response_url = str(getattr(response, "url", url) or url)
        return cls._parse_teamlyzer_profile(
            getattr(response, "text", ""), response_url, expected_name, allow_alias=allow_alias
        )

    @classmethod
    async def fetch_dynamic_teamlyzer(
        cls, company_name: str, client: httpx.AsyncClient
    ) -> Optional[ScoreResult]:
        """Descobre o perfil Teamlyzer pelo autocomplete e valida a pagina final."""
        company_name = " ".join(str(company_name).split()).strip()
        if not company_name:
            return None

        candidate_slugs: List[Tuple[str, bool]] = []
        seen_slugs = set()

        def add_slug(slug: str, allow_alias: bool = False) -> None:
            slug = cls._slugify(slug)
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                candidate_slugs.append((slug, allow_alias))

        # A descoberta por autocomplete e a fonte principal do slug oficial.
        for query in cls._name_variants(company_name):
            endpoint = f"{cls.TEAMLYZER_AUTOCOMPLETE_URL}{urllib.parse.quote(query, safe='')}"
            try:
                response = await client.get(endpoint, timeout=3.5)
            except (httpx.RequestError, asyncio.TimeoutError) as error:
                raise TeamlyzerUnavailable(str(error)) from error

            status = int(getattr(response, "status_code", 0) or 0)
            if status in {403, 429} or status >= 500:
                raise TeamlyzerUnavailable(f"Teamlyzer autocomplete respondeu HTTP {status}")
            if status != 200:
                continue

            payload = cls._response_json(response)
            ranked = sorted(
                cls._autocomplete_candidates(payload),
                key=lambda item: cls._company_similarity(company_name, item[0]),
                reverse=True,
            )
            for candidate_name, slug in ranked[:8]:
                if cls._company_similarity(company_name, candidate_name) >= 0.45:
                    add_slug(slug)

            # O resultado com o nome mais proximo e suficiente para a consulta;
            # as variantes seguintes ficam como fallback para nomes compostos.
            if candidate_slugs:
                break

        special_slug = cls._special_alias_slug(company_name)
        if special_slug:
            add_slug(special_slug, allow_alias=True)

        # Se o autocomplete nao encontrou nada, testa slugs derivados e variantes
        # conhecidas. Isto resolve, por exemplo, Neotalent Conclusion -> neotalent.
        for variant in cls._name_variants(company_name):
            add_slug(variant)
            add_slug(f"{variant} Portugal")
            add_slug(f"{variant} Technologies")
            add_slug(f"{variant} Solutions")

        for slug, allow_alias in candidate_slugs:
            result = await cls._fetch_teamlyzer_profile(client, slug, company_name, allow_alias=allow_alias)
            if result:
                return result

        return None

    @classmethod
    def _glassdoor_search_url(cls, company_name: str) -> str:
        display_name = " ".join(str(company_name).split()).strip()
        return cls.GLASSDOOR_SEARCH_URL.format(urllib.parse.quote(display_name, safe=""))

    @classmethod
    async def fetch_dynamic_glassdoor(
        cls, company_name: str, client: httpx.AsyncClient
    ) -> Optional[ScoreResult]:
        """Procura evidencia corporativa no Glassdoor e devolve sempre link canonico."""
        display_name = " ".join(str(company_name).split()).strip()
        glassdoor_search_url = cls._glassdoor_search_url(display_name)
        query = f'site:glassdoor.com/Overview "{display_name}"'
        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query, safe='')}"

        try:
            response = await client.get(search_url, timeout=5.0)
            if int(getattr(response, "status_code", 0) or 0) == 200:
                soup = bs4.BeautifulSoup(getattr(response, "text", ""), "html.parser")
                for result in soup.select(".result")[:8]:
                    link = result.select_one("a.result__a")
                    href = str(link.get("href", "")) if link else ""
                    try:
                        parsed = urllib.parse.urlparse(href)
                    except ValueError:
                        continue
                    if "glassdoor.com" not in parsed.netloc.lower():
                        continue
                    if not re.search(r"/(?:Overview|Reviews|Jobs|Salary)/", parsed.path, re.IGNORECASE):
                        continue

                    text = result.get_text(" ", strip=True)
                    rating_match = re.search(
                        r"rating\s+(?:of\s+)?([1-5](?:[.,][0-9])?)\s+out\s+of\s+5"
                        r"(?:\s+stars)?(?:[^\d]*(\d[\d,.]*[kK]?)\s+company\s+reviews)?",
                        text,
                        re.IGNORECASE,
                    )
                    if not rating_match:
                        rating_match = re.search(
                            r"\b([1-5](?:[.,][0-9])?)\s*(?:/5|stars|★)\b", text, re.IGNORECASE
                        )
                    if rating_match:
                        score_value = rating_match.group(1).replace(",", ".")
                        review_value = rating_match.group(2) if rating_match.lastindex and rating_match.lastindex >= 2 else None
                        return {
                            "platform": "Glassdoor",
                            "score": f"{float(score_value):.1f}/5",
                            "numeric": float(score_value),
                            "reviews": f"{review_value} Reviews" if review_value else "Glassdoor Reviews",
                            "url": glassdoor_search_url,
                        }
        except (httpx.RequestError, asyncio.TimeoutError, ValueError) as error:
            logger.warning("Glassdoor indisponivel para %s: %s", company_name, error)

        # Sem evidencias confiaveis, nao inventa uma nota. O link permite a
        # verificacao manual e mantem a empresa como "sem rating".
        return {
            "platform": "Glassdoor",
            "score": "Ver Reviews",
            "numeric": 0.0,
            "reviews": "Pesquisa Global",
            "url": glassdoor_search_url,
        }

    @classmethod
    async def get_score_async(
        cls, company_name: str, client: httpx.AsyncClient
    ) -> Optional[ScoreResult]:
        company_name = " ".join(str(company_name or "").split()).strip()
        if cls._normalize_name(company_name) in {cls._normalize_name(value) for value in cls.IGNORED_COMPANIES}:
            return None

        cls.load_cache()
        cache_key = cls._cache_key(company_name)
        cached = cls._cache.get(cache_key)
        legacy_teamlyzer_cache = None

        # Um Teamlyzer validado pode ser reutilizado. Um Glassdoor antigo tem de
        # passar novamente pelo Teamlyzer para corrigir perfis que entretanto foram
        # criados ou que antes falharam por causa de um nome composto.
        if isinstance(cached, dict) and str(cached.get("platform", "")).lower() == "teamlyzer":
            if not cached.get("matched_name"):
                # Entradas criadas pela versao antiga nao guardavam o nome que foi
                # validado. Nao sao confiaveis: revalidar evita perpetuar um perfil
                # de outra empresa encontrado por uma palavra em comum.
                if cls._legacy_teamlyzer_cache_is_plausible(company_name, cached):
                    return cached
                cached = None
                cls._cache.pop(cache_key, None)
            elif cls._trusted_teamlyzer_cache(company_name, cached):
                return cached
            else:
                cls._cache.pop(cache_key, None)
                cached = None
        if isinstance(cached, dict) and cls._teamlyzer_was_checked_recently(cached):
            return cached

        try:
            teamlyzer_result = await cls.fetch_dynamic_teamlyzer(company_name, client)
        except TeamlyzerUnavailable as error:
            logger.warning("Teamlyzer indisponivel para %s: %s", company_name, error)
            if isinstance(cached, dict):
                return cached
            return legacy_teamlyzer_cache

        if teamlyzer_result:
            cls._cache[cache_key] = teamlyzer_result
            cls.save_cache()
            return teamlyzer_result

        if isinstance(cached, dict):
            cached = dict(cached)
            cached["teamlyzer_checked_at"] = int(time.time())
            cls._cache[cache_key] = cached
            cls.save_cache()
            return cached

        glassdoor_result = await cls.fetch_dynamic_glassdoor(company_name, client)
        if glassdoor_result:
            glassdoor_result = dict(glassdoor_result)
            glassdoor_result["teamlyzer_checked_at"] = int(time.time())
            cls._cache[cache_key] = glassdoor_result
            cls.save_cache()
        return glassdoor_result

    @classmethod
    async def enrich_jobs_async(cls, jobs: list) -> None:
        """Enriquece empresas unicas e deixa os jobs prontos para filtragem final."""
        if not jobs:
            return

        cls.load_cache()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        }

        company_names: Dict[str, str] = {}
        for job in jobs:
            company_names.setdefault(cls._cache_key(job.company), job.company)

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            semaphore = asyncio.Semaphore(8)

            async def lookup(name: str) -> Optional[ScoreResult]:
                async with semaphore:
                    return await cls.get_score_async(name, client)

            tasks = [lookup(name) for name in company_names.values()]
            score_results = await asyncio.gather(*tasks, return_exceptions=True)
            scores_by_key = {
                key: result
                for key, result in zip(company_names.keys(), score_results)
                if not isinstance(result, Exception)
            }

            for key, result in zip(company_names.keys(), score_results):
                if isinstance(result, Exception):
                    logger.warning("Falha ao classificar %s: %s", company_names[key], result)
                    scores_by_key[key] = None

            for job in jobs:
                score_result = scores_by_key.get(cls._cache_key(job.company))
                if not isinstance(score_result, dict) or not score_result:
                    job.rating_score = 0.0
                    job.company_score = None
                    job.company_reviews = None
                    job.teamlyzer_url = None
                    continue

                platform = str(score_result.get("platform", "Ranking"))
                score = str(score_result.get("score", "Ver Reviews"))
                reviews = str(score_result.get("reviews", ""))
                try:
                    numeric = float(score_result.get("numeric", 0.0) or 0.0)
                except (TypeError, ValueError):
                    numeric = 0.0

                job.rating_score = numeric
                if score == "Ver Reviews":
                    job.company_score = f"🔍 {platform}"
                else:
                    job.company_score = f"★ {score} ({platform})"
                job.company_reviews = reviews
                job.teamlyzer_url = score_result.get("url")

        cls.save_cache()
