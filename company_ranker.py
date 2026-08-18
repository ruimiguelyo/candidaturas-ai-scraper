import httpx
import bs4
import re
import urllib.parse
import asyncio
import logging
from typing import Optional, Dict

logger = logging.getLogger("CompanyRanker")

class CompanyRanker:
    """Cruza o nome da empresa com o Teamlyzer (Portugal) e com o Glassdoor / Kununu (Internacional)."""

    _cache: Dict[str, Optional[Dict[str, str]]] = {}
    
    # Mapeamento de empresas conhecidas em Portugal e multinacionais
    KNOWN_ALIASES = {
        "innowave": "innowave-technologies",
        "primeit": "prime-it",
        "prime it": "prime-it",
        "santander": "santander-totta",
        "banco santander": "santander-totta",
        "volkswagen": "volkswagen-digital-solutions",
        "volkswagen group": "volkswagen-digital-solutions",
        "philip morris": "tabaqueira",
        "philip morris international": "tabaqueira",
        "critical techworks": "critical-techworks",
        "critical software": "critical-software",
        "feedzai": "feedzai",
        "unbabel": "unbabel",
        "revolut": "revolut",
        "zendesk": "zendesk",
        "alten": "alten",
        "nordea": "nordea-asset-management",
        "nordea asset management": "nordea-asset-management",
        "nordea asset management portugal": "nordea-asset-management",
        "mckinsey": "mckinsey-company",
        "mckinsey & company": "mckinsey-company",
        "outsystems": "outsystems",
        "talkdesk": "talkdesk",
        "defined.ai": "defined-ai",
        "sword health": "sword-health",
        "celfocus": "celfocus",
        "devoteam": "devoteam",
        "imaginary cloud": "imaginary-cloud",
        "closer consulting": "closer-consulting",
        "bnp paribas": "bnp-paribas",
        "siemens": "siemens-sa",
        "bosch": "bosch-portugal",
        "havi": "havi-portugal",
        "havi techhub": "havi-portugal",
        "ceiia": "ceiia",
        "sage": "sage"
    }

    @classmethod
    async def fetch_teamlyzer_score(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        comp_clean = company_name.strip()
        comp_lower = comp_clean.lower()
        
        slug = None
        for alias, target_slug in cls.KNOWN_ALIASES.items():
            if alias == comp_lower or alias in comp_lower or comp_lower in alias:
                slug = target_slug
                break

        if not slug:
            slug = re.sub(r"[^a-z0-9]+", "-", comp_lower).strip("-")

        url = f"https://pt.teamlyzer.com/companies/{slug}"
        try:
            res = await client.get(url, timeout=3.5, follow_redirects=True)
            if res.status_code == 200:
                soup = bs4.BeautifulSoup(res.text, "html.parser")
                score_el = soup.find("span", class_="b_rating") or soup.find("div", class_="score-value-wrapper")
                reviews_el = soup.find("span", class_="label_rating_font")

                if score_el:
                    score_text = score_el.get_text(strip=True)
                    if "/5" not in score_text:
                        score_text = f"{score_text}/5"

                    reviews_text = reviews_el.get_text(strip=True) if reviews_el else ""
                    return {
                        "platform": "Teamlyzer",
                        "score": score_text,
                        "reviews": reviews_text,
                        "url": str(res.url)
                    }
        except Exception:
            pass
        return None

    @classmethod
    async def fetch_glassdoor_score(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        """Fallback para empresas internacionais via Glassdoor."""
        clean_comp = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|operations)$", "", company_name, flags=re.IGNORECASE).strip()
        if not clean_comp:
            clean_comp = company_name

        query = f"{clean_comp} glassdoor employee rating reviews"
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        glassdoor_search_url = f"https://www.glassdoor.com/Search/results.htm?keyword={urllib.parse.quote(clean_comp)}"

        try:
            res = await client.get(url, timeout=4.0)
            if res.status_code == 200:
                soup = bs4.BeautifulSoup(res.text, "html.parser")
                for res_div in soup.find_all("div", class_="result")[:4]:
                    snippet_el = res_div.find("a", class_="result__snippet")
                    if not snippet_el:
                        continue
                    snippet = snippet_el.get_text(" ", strip=True)

                    # 1. Padrão detalhado: 'rating of 3.5 out of 5 stars, based on 1,746 company reviews'
                    m = re.search(r"rating\s+(?:of\s+)?([1-5]\.[0-9])\s+out\s+of\s+5\s+stars(?:[^\d]*([\d,]+)\s+company\s+reviews)?", snippet, re.IGNORECASE)
                    if m:
                        score = m.group(1)
                        rev_count = m.group(2)
                        reviews = f"{rev_count} Reviews" if rev_count else "Glassdoor Reviews"
                        return {
                            "platform": "Glassdoor",
                            "score": f"{score}/5",
                            "reviews": reviews,
                            "url": glassdoor_search_url
                        }

                    # 2. Padrão direto: '3.4 out of 5 stars' ou '3.8/5'
                    m2 = re.search(r"\b([1-5]\.[0-9])\s*(?:out of 5|/5|\s+stars)", snippet, re.IGNORECASE)
                    if m2:
                        return {
                            "platform": "Glassdoor",
                            "score": f"{m2.group(1)}/5",
                            "reviews": "Glassdoor",
                            "url": glassdoor_search_url
                        }
        except Exception:
            pass

        # Se não encontrar nota exata, retorna link para pesquisa no Glassdoor
        return {
            "platform": "Glassdoor",
            "score": "Ver Reviews",
            "reviews": "Pesquisa Global",
            "url": glassdoor_search_url
        }

    @classmethod
    async def get_score_async(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        if not company_name or company_name.lower() in ["n/a", "confidencial", "consultar anúncio", "landing.jobs partner"]:
            return None

        comp_clean = company_name.strip()
        comp_key = re.sub(r"[^a-z0-9]", "", comp_clean.lower())

        if comp_key in cls._cache:
            return cls._cache[comp_key]

        # 1. Tenta Teamlyzer (Portugal e empresas com filial em PT)
        teamlyzer_result = await cls.fetch_teamlyzer_score(comp_clean, client)
        if teamlyzer_result:
            cls._cache[comp_key] = teamlyzer_result
            return teamlyzer_result

        # 2. Fallback: Glassdoor (Multinacionais / Remotas)
        glassdoor_result = await cls.fetch_glassdoor_score(comp_clean, client)
        if glassdoor_result:
            cls._cache[comp_key] = glassdoor_result
            return glassdoor_result

        cls._cache[comp_key] = None
        return None

    @classmethod
    async def enrich_jobs_async(cls, jobs: list):
        """Enriquece uma lista de vagas com scores do Teamlyzer ou Glassdoor."""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
        async with httpx.AsyncClient(headers=headers) as client:
            tasks = [cls.get_score_async(job.company, client) for job in jobs]
            scores = await asyncio.gather(*tasks, return_exceptions=True)

            for job, score_res in zip(jobs, scores):
                if isinstance(score_res, dict) and score_res:
                    platform = score_res.get("platform", "Ranking")
                    score = score_res.get("score", "")
                    reviews = score_res.get("reviews", "")
                    
                    if score == "Ver Reviews":
                        job.company_score = f"🔍 {platform}"
                    else:
                        job.company_score = f"★ {score} ({platform})"
                    
                    job.company_reviews = reviews
                    job.teamlyzer_url = score_res.get("url")
