import httpx
import bs4
import re
import urllib.parse
import asyncio
import logging
from typing import Optional, Dict

logger = logging.getLogger("CompanyRanker")

class CompanyRanker:
    """Cruza a empresa com Teamlyzer e Glassdoor, devolvendo score numérico para ordenação."""

    _cache: Dict[str, Optional[Dict[str, any]]] = {}
    
    # Mapeamento estático de empregadores de topo com scores verificados
    VERIFIED_GLOBAL_TOP_TIER = {
        "cloudflare": {"score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "3.5k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Cloudflare-EI_IE423939.htm"},
        "google": {"score": "4.4/5", "numeric": 4.4, "platform": "Glassdoor", "reviews": "100k+ Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Google-EI_IE9079.htm"},
        "microsoft": {"score": "4.3/5", "numeric": 4.3, "platform": "Glassdoor", "reviews": "90k+ Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Microsoft-EI_IE1651.htm"},
        "stripe": {"score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "1.8k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Stripe-EI_IE671932.htm"},
        "datadog": {"score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "1.2k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Datadog-EI_IE762128.htm"},
        "spotify": {"score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "2.8k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Spotify-EI_IE408251.htm"},
        "meta": {"score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "35k+ Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Meta-EI_IE40772.htm"},
        "apple": {"score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "50k+ Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Apple-EI_IE1138.htm"},
        "uber": {"score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "18k+ Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Uber-EI_IE575263.htm"},
        "github": {"score": "4.4/5", "numeric": 4.4, "platform": "Glassdoor", "reviews": "1.1k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-GitHub-EI_IE671945.htm"},
        "snowflake": {"score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "1.5k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Snowflake-EI_IE919246.htm"},
        "canonical": {"score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "1.8k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Canonical-EI_IE31046.htm"},
        "peraton": {"score": "3.5/5", "numeric": 3.5, "platform": "Glassdoor", "reviews": "1.7k Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Peraton-EI_IE1865320.htm"},
        "smule": {"score": "3.4/5", "numeric": 3.4, "platform": "Glassdoor", "reviews": "144 Reviews", "url": "https://www.glassdoor.com/Overview/Working-at-Smule-EI_IE317805.htm"},
        "cambium learning": {"score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "113 Reviews", "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cambium%20Learning"},
        "cambium learning group": {"score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "113 Reviews", "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cambium%20Learning"},
        "seven senders": {"score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "40 Reviews", "url": "https://www.glassdoor.com/Search/results.htm?keyword=Seven%20Senders"},
        "seven senders gmbh": {"score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "40 Reviews", "url": "https://www.glassdoor.com/Search/results.htm?keyword=Seven%20Senders"}
    }

    # Mapeamento estático de empregadores em Portugal no Teamlyzer
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
    async def fetch_teamlyzer_score(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
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
    async def fetch_glassdoor_score(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        comp_lower = company_name.lower().strip()
        
        # 1. Verifica base verificada de gigantes tecnológicas
        for verified_name, data in cls.VERIFIED_GLOBAL_TOP_TIER.items():
            if verified_name in comp_lower or comp_lower in verified_name:
                return data

        clean_comp = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|operations)$", "", company_name, flags=re.IGNORECASE).strip()
        if not clean_comp:
            clean_comp = company_name

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

                    m2 = re.search(r"\b([1-5]\.[0-9])\s*(?:out of 5|/5|\s+stars)", snippet, re.IGNORECASE)
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

        comp_clean = company_name.strip()
        comp_key = re.sub(r"[^a-z0-9]", "", comp_clean.lower())

        if comp_key in cls._cache:
            return cls._cache[comp_key]

        # 1. Verifica Top Tier Verificado
        for verified_name, data in cls.VERIFIED_GLOBAL_TOP_TIER.items():
            if verified_name in comp_clean.lower() or comp_clean.lower() in verified_name:
                cls._cache[comp_key] = data
                return data

        # 2. Tenta Teamlyzer
        teamlyzer_result = await cls.fetch_teamlyzer_score(comp_clean, client)
        if teamlyzer_result:
            cls._cache[comp_key] = teamlyzer_result
            return teamlyzer_result

        # 3. Fallback Glassdoor
        glassdoor_result = await cls.fetch_glassdoor_score(comp_clean, client)
        if glassdoor_result:
            cls._cache[comp_key] = glassdoor_result
            return glassdoor_result

        cls._cache[comp_key] = None
        return None

    @classmethod
    async def enrich_jobs_async(cls, jobs: list):
        """Enriquece as vagas com os scores e define a nota numérica para ordenação."""
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
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
