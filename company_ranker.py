import httpx
import bs4
import re
import urllib.parse
import asyncio
import json
import os
import logging
from typing import Optional, Dict

logger = logging.getLogger("CompanyRanker")

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_scores_cache.json")

class CompanyRanker:
    """
    Motor Dinâmico e Future-Proof de Ranking de Empresas:
    - Prioridade 1: Pesquisa Dinâmica no Teamlyzer Portugal (com suporte a slugs dinâmicos e aliases).
    - Prioridade 2: Fallback Dinâmico para Glassdoor Global (apenas se não existir no Teamlyzer).
    - Cache Persistente em JSON (`company_scores_cache.json`) para evitar queries repetidas e bloqueios de rede.
    """

    _cache: Dict[str, Optional[Dict[str, any]]] = {}
    _cache_loaded: bool = False

    # Mapeamento conhecido para casos especiais de nomes em Portugal (onde o nome da vaga difere da entidade registada)
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
        "nordea": "nordea-asset-management",
        "nordea asset management": "nordea-asset-management",
        "nordea asset management portugal": "nordea-asset-management"
    }

    @classmethod
    def load_cache(cls):
        """Carrega a cache persistente do disco."""
        if cls._cache_loaded:
            return
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cls._cache = json.load(f)
            except Exception as e:
                logger.warning(f"Erro ao carregar cache de rankings: {e}")
        cls._cache_loaded = True

    @classmethod
    def save_cache(cls):
        """Salva a cache persistente no disco."""
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cls._cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Erro ao salvar cache de rankings: {e}")

    @classmethod
    def _normalize_name(cls, name: str) -> str:
        clean = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|operations|portugal|sa|s\.a\.)$", "", name, flags=re.IGNORECASE).strip()
        clean = clean.replace("&", "and").strip()
        return clean if clean else name.strip()

    @classmethod
    async def fetch_dynamic_teamlyzer(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        """Pesquisa dinamicamente no Teamlyzer Portugal usando slugs derivados e aliases."""
        comp_lower = company_name.lower().strip()
        
        # 1. Verificar se temos um alias especial (ex: Philip Morris -> Tabaqueira)
        target_slug = cls.SPECIAL_PORTUGAL_ALIASES.get(comp_lower)
        
        slugs_to_try = []
        if target_slug:
            slugs_to_try.append(target_slug)

        clean_name = cls._normalize_name(company_name)
        base_slug = re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-")
        
        if base_slug and base_slug not in slugs_to_try:
            slugs_to_try.extend([
                base_slug,
                f"{base_slug}-portugal",
                f"{base_slug}-technologies",
                f"{base_slug}-solutions",
                f"{base_slug}-digital"
            ])

        for slug in slugs_to_try:
            url = f"https://pt.teamlyzer.com/companies/{slug}"
            try:
                res = await client.get(url, timeout=3.0, follow_redirects=True)
                if res.status_code == 200 and "pt.teamlyzer.com/companies/" in str(res.url):
                    soup = bs4.BeautifulSoup(res.text, "html.parser")
                    score_el = soup.find("span", class_="b_rating") or soup.find("div", class_="score-value-wrapper")
                    reviews_el = soup.find("span", class_="label_rating_font")

                    if score_el:
                        score_text = score_el.get_text(strip=True)
                        num_match = re.search(r"([1-5]\.[0-9])", score_text)
                        num_val = float(num_match.group(1)) if num_match else 0.0

                        if "/5" not in score_text:
                            score_text = f"{score_text}/5"

                        reviews_text = reviews_el.get_text(strip=True) if reviews_el else ""
                        return {
                            "platform": "Teamlyzer",
                            "score": score_text,
                            "numeric": num_val,
                            "reviews": reviews_text,
                            "url": str(res.url)
                        }
            except Exception:
                pass
        return None

    @classmethod
    async def fetch_dynamic_glassdoor(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        """Pesquisa dinamicamente o Glassdoor via search engine e gera link canónico de pesquisa."""
        clean_comp = cls._normalize_name(company_name)
        query = f"{clean_comp} glassdoor employee rating reviews"
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        glassdoor_search_url = f"https://www.glassdoor.com/Search/results.htm?keyword={urllib.parse.quote(clean_comp)}"

        try:
            res = await client.get(url, timeout=3.5)
            if res.status_code == 200:
                soup = bs4.BeautifulSoup(res.text, "html.parser")
                for res_div in soup.find_all("div", class_="result")[:4]:
                    snippet_el = res_div.find("a", class_="result__snippet")
                    if not snippet_el:
                        continue
                    snippet = snippet_el.get_text(" ", strip=True)

                    m = re.search(r"rating\s+(?:of\s+)?([1-5]\.[0-9])\s+out\s+of\s+5\s+stars(?:[^\d]*([\d,]+)\s+company\s+reviews)?", snippet, re.IGNORECASE)
                    if m:
                        score = m.group(1)
                        rev_count = m.group(2)
                        return {
                            "platform": "Glassdoor",
                            "score": f"{score}/5",
                            "numeric": float(score),
                            "reviews": f"{rev_count} Reviews" if rev_count else "Glassdoor Reviews",
                            "url": glassdoor_search_url
                        }

                    m2 = re.search(r"\b([1-5]\.[0-9])\s*(?:out of 5|/5|\s+stars|\s*★)", snippet, re.IGNORECASE)
                    if m2:
                        score = m2.group(1)
                        return {
                            "platform": "Glassdoor",
                            "score": f"{score}/5",
                            "numeric": float(score),
                            "reviews": "Glassdoor",
                            "url": glassdoor_search_url
                        }
        except Exception:
            pass

        return {
            "platform": "Glassdoor",
            "score": "Ver Reviews",
            "numeric": 0.0,
            "reviews": "Pesquisa Global",
            "url": glassdoor_search_url
        }

    @classmethod
    async def get_score_async(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        if not company_name or company_name.lower() in ["n/a", "confidencial", "consultar anúncio", "landing.jobs partner"]:
            return None

        cls.load_cache()
        comp_key = re.sub(r"[^a-z0-9]", "", company_name.lower())

        if comp_key in cls._cache:
            return cls._cache[comp_key]

        # 1. TENTATIVA 1: TEAMLYZER PORTUGAL (PRIORIDADE ABSOLUTA)
        teamlyzer_result = await cls.fetch_dynamic_teamlyzer(company_name, client)
        if teamlyzer_result:
            cls._cache[comp_key] = teamlyzer_result
            cls.save_cache()
            return teamlyzer_result

        # 2. TENTATIVA 2: GLASSDOOR GLOBAL (APENAS SE NÃO EXISTIR NO TEAMLYZER)
        glassdoor_result = await cls.fetch_dynamic_glassdoor(company_name, client)
        if glassdoor_result:
            cls._cache[comp_key] = glassdoor_result
            cls.save_cache()
            return glassdoor_result

        cls._cache[comp_key] = None
        cls.save_cache()
        return None

    @classmethod
    async def enrich_jobs_async(cls, jobs: list):
        """Enriquece a lista de vagas com os scores e define a nota numérica para ordenação decrescente."""
        cls.load_cache()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"
        }
        async with httpx.AsyncClient(headers=headers) as client:
            tasks = [cls.get_score_async(job.company, client) for job in jobs]
            scores = await asyncio.gather(*tasks, return_exceptions=True)

            for job, score_res in zip(jobs, scores):
                if isinstance(score_res, dict) and score_res:
                    platform = score_res.get("platform", "Ranking")
                    score = score_res.get("score", "")
                    reviews = score_res.get("reviews", "")
                    num_val = float(score_res.get("numeric", 0.0))
                    
                    job.rating_score = num_val
                    if score == "Ver Reviews":
                        job.company_score = f"🔍 {platform}"
                    else:
                        job.company_score = f"★ {score} ({platform})"
                    
                    job.company_reviews = reviews
                    job.teamlyzer_url = score_res.get("url")
                else:
                    job.rating_score = 0.0
        
        cls.save_cache()
