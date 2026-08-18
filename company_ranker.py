import httpx
import bs4
import re
import asyncio
import logging
from typing import Optional, Dict

logger = logging.getLogger("CompanyRanker")

class CompanyRanker:
    """Cruza as empresas com os seus scores e reviews no Teamlyzer de forma assíncrona."""

    _cache: Dict[str, Optional[Dict[str, str]]] = {}
    
    # Mapeamento estático rigoroso dos empregadores de tecnologia em Portugal
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
    async def get_score_async(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        if not company_name or company_name.lower() in ["n/a", "confidencial", "consultar anúncio", "landing.jobs partner"]:
            return None

        comp_clean = company_name.strip()
        comp_lower = comp_clean.lower()
        comp_key = re.sub(r"[^a-z0-9]", "", comp_lower)

        if comp_key in cls._cache:
            return cls._cache[comp_key]

        # 1. Verificar Alias Conhecido
        slug = None
        for alias, target_slug in cls.KNOWN_ALIASES.items():
            if alias == comp_lower or alias in comp_lower or comp_lower in alias:
                slug = target_slug
                break

        # Fallback de slug normalizado
        if not slug:
            clean_slug = re.sub(r"[^a-z0-9]+", "-", comp_lower).strip("-")
            slug = clean_slug

        # 2. Consultar página da empresa no Teamlyzer
        url = f"https://pt.teamlyzer.com/companies/{slug}"
        try:
            res = await client.get(url, timeout=4.0, follow_redirects=True)
            if res.status_code == 200:
                soup = bs4.BeautifulSoup(res.text, "html.parser")
                score_el = soup.find("span", class_="b_rating") or soup.find("div", class_="score-value-wrapper")
                reviews_el = soup.find("span", class_="label_rating_font")

                if score_el:
                    score_text = score_el.get_text(strip=True)
                    if "/5" not in score_text:
                        score_text = f"{score_text}/5"

                    reviews_text = reviews_el.get_text(strip=True) if reviews_el else ""
                    result = {
                        "platform": "Teamlyzer",
                        "score": score_text,
                        "reviews": reviews_text,
                        "url": str(res.url)
                    }
                    cls._cache[comp_key] = result
                    return result
        except Exception:
            pass

        cls._cache[comp_key] = None
        return None

    @classmethod
    async def enrich_jobs_async(cls, jobs: list):
        """Enriquece uma lista de vagas com scores em paralelo ultrarrápido."""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
        async with httpx.AsyncClient(headers=headers) as client:
            tasks = [cls.get_score_async(job.company, client) for job in jobs]
            scores = await asyncio.gather(*tasks, return_exceptions=True)

            for job, score_res in zip(jobs, scores):
                if isinstance(score_res, dict) and score_res:
                    job.company_score = f"★ {score_res['score']}"
                    job.company_reviews = score_res.get("reviews")
                    job.teamlyzer_url = score_res.get("url")
