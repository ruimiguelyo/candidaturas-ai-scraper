"""Inteligência de empresas via Teamlyzer Portugal + Base de Conhecimento Curada."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

import bs4
import httpx

from estagio_radar.config_loader import load_companies_config
from estagio_radar.models import CompanyProfile, CompanyType, RawJob

logger = logging.getLogger("estagio_radar.company_intel")

TEAMLYZER_BASE_URL = "https://pt.teamlyzer.com"
TEAMLYZER_AUTOCOMPLETE_URL = f"{TEAMLYZER_BASE_URL}/users/autocomplete_company/v2/"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 dias
BREAKER_THRESHOLD = 4

SECONDARY_SUFFIXES = {
    "portugal", "pt", "sa", "s.a", "lda", "unipessoal", "gmbh", "inc", "llc", "ltd",
    "limited", "corp", "corporation", "group", "solutions", "technologies", "technology",
    "services", "consulting", "conclusion", "cib", "europe", "emea", "iberia",
}

RESEARCH_KEYWORDS = ("inesc", "fraunhofer", "champalimaud", "instituto", "university", "universidade", "lab")
FINANCE_ENERGY_KEYWORDS = (
    "banco", "bank", "santander", "millennium", "bnp", "natixis", "fidelidade",
    "edp", "galp", "nos", "vodafone", "meo", "nordea", "credibom", "euronext",
)


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def base_company_name(name: str) -> str:
    """Extrai a marca principal quando a fonte usa sub-divisões como 'Devoteam | Cyber Trust'."""
    raw = str(name or "").strip()
    if "|" in raw:
        head = raw.split("|", 1)[0].strip()
        if len(head) >= 2:
            return head
    return raw


def normalize_company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(name).casefold())


def normalize_company_words(name: str) -> str:
    clean = strip_accents(base_company_name(name)).casefold().strip()
    clean = re.sub(r"\[[^\]]*\]", " ", clean)  # Remove "[Portugal]" etc.
    clean = re.sub(r"\bin\s+(?:portugal|lisbon|lisboa|europe|emea)\b", " ", clean)
    clean = clean.replace("&", " and ")
    clean = re.sub(r"\bs\.?\s*a\.?\b", " ", clean)
    clean = re.sub(r"[(),.;|/]+", " ", clean)
    tokens = re.findall(r"[a-z0-9]+", clean)
    while len(tokens) > 1 and tokens[-1] in SECONDARY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or strip_accents(name).casefold().strip()


def slugify_company(name: str) -> str:
    clean = strip_accents(name).casefold().replace("&", " and ")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", clean)).strip("-")


def compute_bayesian_rating(rating: float | None, reviews: int) -> float | None:
    if rating is None or rating <= 0:
        return None
    m = 10.0
    c = 3.2
    v = max(0, int(reviews))
    return round((v * rating + m * c) / (v + m), 2)


class TeamlyzerUnavailable(RuntimeError):
    """Sinaliza indisponibilidade temporária do Teamlyzer (403/429/5xx/timeout)."""


class CompanyIntelService:
    """Resolve reputação, salários médios, tipo (Produto vs Consultora) e tier."""

    def __init__(self, cache_path: str | Path = "data/company_cache.json"):
        self.cache_path = Path(cache_path)
        self.config = load_companies_config()
        self._tier_s = {normalize_company_key(x) for x in self.config.get("tier_s", [])}
        self._tier_a = {normalize_company_key(x) for x in self.config.get("tier_a", [])}
        self._consultancies = {normalize_company_key(x) for x in self.config.get("consultancies", [])}
        self._aliases: dict[str, str] = {
            " ".join(strip_accents(k).casefold().split()): str(v)
            for k, v in (self.config.get("teamlyzer_aliases") or {}).items()
        }
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            self._cache = {}
            return
        try:
            loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError) as err:
            logger.warning("Cache Teamlyzer inválida (%s); iniciando vazia.", err)
            self._cache = {}

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.cache_path.parent),
                prefix="company_cache.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
                json.dump(self._cache, tmp, indent=2, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.cache_path)
        except OSError as err:
            logger.warning("Falha ao gravar cache Teamlyzer: %s", err)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _is_known_consultancy(self, company_name: str, categories: list[str]) -> bool:
        brand = base_company_name(company_name)
        key = normalize_company_key(brand)
        words_key = normalize_company_key(normalize_company_words(brand))
        if key in self._consultancies or words_key in self._consultancies:
            return True
        if any(c in key for c in self._consultancies if len(c) >= 6):
            return True
        for cat in categories:
            if "consultoras-outsourcing" in cat.casefold():
                return True
        if (
            not self._match_tier_set(brand, self._tier_s | self._tier_a)
            and re.search(r"\b(?:consulting|consultoria|outsourcing)\b", strip_accents(brand), re.I)
        ):
            return True
        return False

    def _match_tier_set(self, company_name: str, tier_set: set[str]) -> bool:
        brand = base_company_name(company_name)
        key = normalize_company_key(brand)
        words_key = normalize_company_key(normalize_company_words(brand))
        if key in tier_set or words_key in tier_set:
            return True
        return any(
            (t == key or t == words_key or (len(t) >= 6 and t in key))
            for t in tier_set
        )

    def _name_similarity(self, expected: str, candidate: str) -> float:
        e_norm = normalize_company_words(expected)
        c_norm = normalize_company_words(candidate)
        if not e_norm or not c_norm:
            return 0.0
        if e_norm == c_norm:
            return 1.0
        e_tokens = set(e_norm.split())
        c_tokens = set(c_norm.split())
        if e_tokens and e_tokens.issubset(c_tokens):
            return 0.85
        return 0.0

    def _name_variants(self, company_name: str) -> list[str]:
        clean = " ".join(str(company_name).split()).strip()
        norm = normalize_company_words(clean)
        variants: list[str] = []
        for v in (clean, clean.replace("&", "and"), norm):
            v_clean = " ".join(v.split()).strip()
            if v_clean and v_clean not in variants:
                variants.append(v_clean)
        tokens = norm.split()
        if len(tokens) > 1:
            variants.append(tokens[0])
        return variants

    @staticmethod
    def parse_teamlyzer_profile(
        html: str,
        response_url: str,
        expected_name: str,
        *,
        allow_alias: bool = False,
    ) -> dict[str, Any] | None:
        """Extrai rating, nº reviews, categorias (ex: consultoras) e salário médio do HTML."""
        soup = bs4.BeautifulSoup(html or "", "html.parser")
        canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
        canonical_href = str(canonical_tag.get("href", "")) if canonical_tag else response_url
        parsed = urllib.parse.urlparse(canonical_href or response_url)
        slug_match = re.fullmatch(r"/companies/([^/?#]+)/?", parsed.path)
        if not slug_match:
            return None
        slug = slug_match.group(1)
        canonical_url = f"{TEAMLYZER_BASE_URL}/companies/{slug}"

        heading = soup.select_one("h1.reduce-h1") or soup.select_one(".company_header_basic_info h1")
        heading_name = heading.get_text(" ", strip=True) if heading else ""
        name_meta = soup.select_one('span[itemprop="itemReviewed"] meta[itemprop="name"]')
        profile_name = heading_name or (str(name_meta.get("content", "")).strip() if name_meta else "")
        if not profile_name or re.search(r"erro\s*404", profile_name, re.I):
            return None

        if not allow_alias:
            e_norm = normalize_company_words(expected_name)
            c_norm = normalize_company_words(profile_name)
            if not e_norm or not c_norm:
                return None
            e_tokens = set(e_norm.split())
            c_tokens = set(c_norm.split())
            if e_norm != c_norm and not e_tokens.issubset(c_tokens) and not c_tokens.issubset(e_tokens):
                return None

        rating_meta = soup.select_one('meta[itemprop="ratingValue"]')
        review_meta = soup.select_one('meta[itemprop="reviewCount"]')

        rating_val: float | None = None
        if rating_meta and rating_meta.get("content"):
            m = re.search(r"([1-5](?:[.,]\d+)?)", str(rating_meta["content"]))
            if m:
                rating_val = float(m.group(1).replace(",", "."))
        if rating_val is None:
            vis = (
                soup.select_one(".score-value-wrapper .c_rating")
                or soup.select_one(".c_rating")
                or soup.select_one(".b_rating")
            )
            if vis:
                m = re.search(r"([1-5](?:[.,]\d+)?)", vis.get_text(" ", strip=True))
                if m:
                    rating_val = float(m.group(1).replace(",", "."))

        reviews_count = 0
        if review_meta and review_meta.get("content"):
            m = re.search(r"(\d+)", str(review_meta["content"]))
            if m:
                reviews_count = int(m.group(1))
        else:
            vis_rev = soup.select_one(".label_rating_font")
            if vis_rev:
                m = re.search(r"(\d+)", vis_rev.get_text(" ", strip=True))
                if m:
                    reviews_count = int(m.group(1))

        # Perfis fantasma 5.0/5 com 0 reviews são artefactos de autocomplete
        if rating_val is not None and rating_val >= 5.0 and reviews_count == 0:
            return None

        categories: list[str] = []
        for a in soup.select('a[href*="/companies/ranking/"]'):
            href = str(a.get("href", ""))
            cm = re.search(r"/companies/ranking/([a-z0-9-]+)", href)
            if cm and cm.group(1) not in categories:
                categories.append(cm.group(1))

        # Extrair salário bruto anual middle/senior (ex: "64.400€")
        avg_salary_eur: int | None = None
        sal_match = re.search(r"(\d{2,3}\.\d{3})\s*€", html)
        if sal_match:
            try:
                candidate_sal = int(sal_match.group(1).replace(".", ""))
                if 12000 <= candidate_sal <= 250000:
                    avg_salary_eur = candidate_sal
            except ValueError:
                pass

        return {
            "teamlyzer_slug": slug,
            "teamlyzer_url": canonical_url,
            "matched_name": profile_name,
            "teamlyzer_rating": rating_val,
            "teamlyzer_reviews": reviews_count,
            "teamlyzer_categories": categories,
            "avg_salary_eur": avg_salary_eur,
        }

    async def _fetch_profile(
        self, client: httpx.AsyncClient, slug: str, expected_name: str, *, allow_alias: bool = False
    ) -> dict[str, Any] | None:
        url = f"{TEAMLYZER_BASE_URL}/companies/{slug}"
        try:
            res = await client.get(url, timeout=5.0, follow_redirects=True)
        except (httpx.RequestError, asyncio.TimeoutError) as err:
            raise TeamlyzerUnavailable(str(err) or type(err).__name__) from err

        if res.status_code in {403, 429} or res.status_code >= 500:
            raise TeamlyzerUnavailable(f"HTTP {res.status_code}")
        if res.status_code != 200:
            return None
        return self.parse_teamlyzer_profile(
            res.text, str(res.url), expected_name, allow_alias=allow_alias
        )

    async def _discover_teamlyzer(
        self, company_name: str, client: httpx.AsyncClient
    ) -> dict[str, Any] | None:
        compact = " ".join(strip_accents(company_name).casefold().split())
        alias_slug = self._aliases.get(compact) or self._aliases.get(normalize_company_words(company_name))

        candidates: list[tuple[str, bool]] = []
        seen: set[str] = set()

        def add(slug: str, allow_alias: bool = False) -> None:
            s = slugify_company(slug)
            if s and s not in seen:
                seen.add(s)
                candidates.append((s, allow_alias))

        if alias_slug:
            add(alias_slug, allow_alias=True)

        for query in self._name_variants(company_name)[:2]:
            endpoint = f"{TEAMLYZER_AUTOCOMPLETE_URL}{urllib.parse.quote(query, safe='')}"
            try:
                res = await client.get(endpoint, timeout=4.0)
            except (httpx.RequestError, asyncio.TimeoutError) as err:
                raise TeamlyzerUnavailable(str(err) or type(err).__name__) from err
            if res.status_code in {403, 429} or res.status_code >= 500:
                raise TeamlyzerUnavailable(f"Autocomplete HTTP {res.status_code}")
            if res.status_code != 200:
                continue
            try:
                payload = res.json()
            except ValueError:
                continue
            items = payload.get("results") or payload.get("json_list") or (payload if isinstance(payload, list) else [])
            if isinstance(items, dict):
                items = list(items.values())
            scored: list[tuple[float, str]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("label") or item.get("title") or "").strip()
                slug = str(item.get("slug") or item.get("value") or "").strip()
                if "/companies/" in slug:
                    slug = slug.split("/companies/", 1)[1].split("/", 1)[0]
                sim = self._name_similarity(company_name, name or slug.replace("-", " "))
                if sim >= 0.8 and slug:
                    scored.append((sim, slug))
            for _, slug in sorted(scored, reverse=True)[:4]:
                add(slug)
            if scored:
                break

        for variant in self._name_variants(company_name)[:2]:
            add(variant)
            add(f"{variant}-portugal")

        for slug, allow_alias in candidates[:5]:
            found = await self._fetch_profile(client, slug, company_name, allow_alias=allow_alias)
            if found:
                return found
        return None

    def _build_profile(
        self,
        company_name: str,
        tl_data: dict[str, Any] | None,
        hint: dict[str, Any] | None = None,
    ) -> CompanyProfile:
        tl = tl_data or {}
        rating = tl.get("teamlyzer_rating")
        reviews = int(tl.get("teamlyzer_reviews") or 0)
        if rating is None and hint:
            rating = hint.get("rating")
            reviews = int(hint.get("reviews") or 0)

        categories: list[str] = list(tl.get("teamlyzer_categories") or [])
        avg_salary = tl.get("avg_salary_eur")
        slug = tl.get("teamlyzer_slug") or (hint.get("slug") if hint else None)
        tl_url = tl.get("teamlyzer_url") or (f"{TEAMLYZER_BASE_URL}/companies/{slug}" if slug else None)

        bayesian = compute_bayesian_rating(rating, reviews)
        is_consultancy = self._is_known_consultancy(company_name, categories)
        norm_lower = normalize_company_words(company_name)

        if is_consultancy:
            tier = "CONSULTANCY"
            ctype = CompanyType.CONSULTANCY
        elif self._match_tier_set(company_name, self._tier_s):
            tier = "S"
            if any(k in norm_lower for k in RESEARCH_KEYWORDS):
                ctype = CompanyType.RESEARCH_ACADEMIC
            else:
                ctype = CompanyType.PRODUCT_TECH
        elif self._match_tier_set(company_name, self._tier_a):
            tier = "A"
            if any(k in norm_lower for k in FINANCE_ENERGY_KEYWORDS):
                ctype = CompanyType.ENTERPRISE_FINANCE
            else:
                ctype = CompanyType.PRODUCT_TECH
        elif "software-house-internet" in categories or (avg_salary and avg_salary >= 42000):
            tier = "B"
            ctype = CompanyType.PRODUCT_TECH
        elif slug or rating is not None:
            tier = "B"
            if any("banca" in c or "seguros" in c or "energia" in c for c in categories) or any(
                k in norm_lower for k in FINANCE_ENERGY_KEYWORDS
            ):
                ctype = CompanyType.ENTERPRISE_FINANCE
            else:
                ctype = CompanyType.UNKNOWN
        else:
            tier = "UNKNOWN"
            ctype = CompanyType.UNKNOWN

        return CompanyProfile(
            company_name=company_name,
            normalized_name=normalize_company_key(company_name),
            teamlyzer_slug=slug,
            teamlyzer_url=tl_url,
            teamlyzer_rating=rating,
            teamlyzer_reviews=reviews,
            bayesian_rating=bayesian,
            avg_salary_eur=avg_salary,
            teamlyzer_categories=categories,
            is_consultancy=is_consultancy,
            company_tier=tier,
            company_type=ctype,
            checked_at=tl.get("checked_at"),
        )

    async def resolve_companies(
        self,
        jobs: Iterable[RawJob],
        *,
        allow_network: bool = True,
    ) -> dict[str, CompanyProfile]:
        """Resolve perfis únicos para todos os RawJobs, respeitando cache e circuit breaker."""
        unique_companies: dict[str, tuple[str, dict[str, Any] | None]] = {}
        for job in jobs:
            key = normalize_company_key(job.company)
            if key and key not in unique_companies:
                unique_companies[key] = (job.company, job.teamlyzer_hint)
            elif key and job.teamlyzer_hint and not unique_companies[key][1]:
                unique_companies[key] = (unique_companies[key][0], job.teamlyzer_hint)

        profiles: dict[str, CompanyProfile] = {}
        now_ts = int(time.time())
        to_fetch: list[tuple[str, str, dict[str, Any] | None]] = []

        for key, (name, hint) in unique_companies.items():
            cached = self._cache.get(key)
            if isinstance(cached, dict) and (now_ts - int(cached.get("checked_at") or 0) < CACHE_TTL_SECONDS):
                profiles[key] = self._build_profile(name, cached if cached.get("teamlyzer_slug") else None, hint)
            else:
                to_fetch.append((key, name, hint))

        if allow_network and to_fetch:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            }
            breaker_open = False
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                for offset in range(0, len(to_fetch), BREAKER_THRESHOLD):
                    batch = to_fetch[offset : offset + BREAKER_THRESHOLD]
                    if breaker_open:
                        for key, name, hint in batch:
                            cached = self._cache.get(key)
                            profiles[key] = self._build_profile(name, cached, hint)
                        continue

                    async def _one(k: str, n: str) -> tuple[str, dict[str, Any] | None | Exception]:
                        try:
                            return k, await self._discover_teamlyzer(n, client)
                        except TeamlyzerUnavailable as err:
                            return k, err
                        except Exception as err:
                            return k, err

                    results = await asyncio.gather(*(_one(k, n) for k, n, _ in batch))
                    unavail_count = 0
                    for (key, name, hint), (_, res) in zip(batch, results):
                        if isinstance(res, TeamlyzerUnavailable):
                            unavail_count += 1
                            cached = self._cache.get(key)
                            profiles[key] = self._build_profile(name, cached, hint)
                        elif isinstance(res, Exception):
                            cached = self._cache.get(key)
                            profiles[key] = self._build_profile(name, cached, hint)
                        elif isinstance(res, dict):
                            entry = {**res, "checked_at": now_ts}
                            self._cache[key] = entry
                            profiles[key] = self._build_profile(name, entry, hint)
                        else:
                            # Não encontrado no Teamlyzer — guarda negative cache de 7d para não repetir
                            self._cache[key] = {"teamlyzer_slug": None, "checked_at": now_ts}
                            profiles[key] = self._build_profile(name, None, hint)

                    if len(batch) == BREAKER_THRESHOLD and unavail_count == BREAKER_THRESHOLD:
                        breaker_open = True
                        logger.warning("Teamlyzer indisponível; a usar cache + config curada para o restante.")

            self.save_cache()
        else:
            for key, name, hint in to_fetch:
                cached = self._cache.get(key)
                profiles[key] = self._build_profile(name, cached, hint)

        return profiles
