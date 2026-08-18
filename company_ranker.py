import httpx
import bs4
import re
import urllib.parse
import asyncio
import logging
from typing import Optional, Dict

logger = logging.getLogger("CompanyRanker")

class CompanyRanker:
    """
    Sistema Robusto e Future-Proof de Ranking e Reviews de Empresas:
    - Prioridade 1: Perfil oficial no Teamlyzer Portugal (para empresas nacionais e multinacionais com filial em PT).
    - Prioridade 2: Perfil oficial e pesquisa canónica no Glassdoor Global (sem IDs voláteis vulneráveis a redirects errados).
    """

    _cache: Dict[str, Optional[Dict[str, any]]] = {}

    # Base de Ratings Verificados (Teamlyzer PT & Glassdoor Global)
    # Nota: URLs do Glassdoor usam endpoints canónicos de busca para garantir que NUNCA redirecionam para outra empresa!
    VERIFIED_COMPANIES = {
        "mckinsey": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "15.6k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=McKinsey%20%26%20Company"
        },
        "mckinsey & company": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "15.6k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=McKinsey%20%26%20Company"
        },
        "cloudflare": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "3.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cloudflare"
        },
        "volkswagen": {
            "score": "3.5/5", "numeric": 3.5, "platform": "Teamlyzer", "reviews": "144 Reviews",
            "url": "https://pt.teamlyzer.com/companies/volkswagen-digital-solutions"
        },
        "volkswagen group": {
            "score": "3.5/5", "numeric": 3.5, "platform": "Teamlyzer", "reviews": "144 Reviews",
            "url": "https://pt.teamlyzer.com/companies/volkswagen-digital-solutions"
        },
        "santander": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "30k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Banco%20Santander"
        },
        "banco santander": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "30k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Banco%20Santander"
        },
        "nordea": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "4.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Nordea%20Asset%20Management"
        },
        "nordea asset management": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "4.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Nordea%20Asset%20Management"
        },
        "nordea asset management portugal": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "4.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Nordea%20Asset%20Management"
        },
        "innowave": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Teamlyzer", "reviews": "147 Reviews",
            "url": "https://pt.teamlyzer.com/companies/innowave-technologies"
        },
        "feedzai": {
            "score": "3.4/5", "numeric": 3.4, "platform": "Teamlyzer", "reviews": "121 Reviews",
            "url": "https://pt.teamlyzer.com/companies/feedzai"
        },
        "critical techworks": {
            "score": "3.0/5", "numeric": 3.0, "platform": "Teamlyzer", "reviews": "641 Reviews",
            "url": "https://pt.teamlyzer.com/companies/critical-techworks"
        },
        "revolut": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "54 Reviews",
            "url": "https://pt.teamlyzer.com/companies/revolut"
        },
        "zendesk": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "2.1k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Zendesk"
        },
        "havi": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "1.2k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=HAVI"
        },
        "havi techhub": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "1.2k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=HAVI"
        },
        "philip morris": {
            "score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "12k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Philip%20Morris%20International"
        },
        "philip morris international": {
            "score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "12k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Philip%20Morris%20International"
        },
        "alten": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "5.6k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=ALTEN"
        },
        "ceiia": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "18 Reviews",
            "url": "https://pt.teamlyzer.com/companies/ceiia"
        },
        "euronext": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "650 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Euronext"
        },
        "zebra technologies": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "3.8k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Zebra%20Technologies"
        },
        "google": {
            "score": "4.4/5", "numeric": 4.4, "platform": "Glassdoor", "reviews": "100k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Google"
        },
        "microsoft": {
            "score": "4.3/5", "numeric": 4.3, "platform": "Glassdoor", "reviews": "90k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Microsoft"
        },
        "stripe": {
            "score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "1.8k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Stripe"
        },
        "datadog": {
            "score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "1.2k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Datadog"
        },
        "spotify": {
            "score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "2.8k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Spotify"
        },
        "meta": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "35k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Meta"
        },
        "apple": {
            "score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "50k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Apple"
        },
        "uber": {
            "score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "18k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Uber"
        },
        "github": {
            "score": "4.4/5", "numeric": 4.4, "platform": "Glassdoor", "reviews": "1.1k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=GitHub"
        },
        "snowflake": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "1.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Snowflake"
        },
        "canonical": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "1.8k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Canonical"
        },
        "peraton": {
            "score": "3.5/5", "numeric": 3.5, "platform": "Glassdoor", "reviews": "1.7k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Peraton"
        },
        "smule": {
            "score": "3.4/5", "numeric": 3.4, "platform": "Glassdoor", "reviews": "144 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Smule"
        },
        "cambium learning": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "113 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cambium%20Learning"
        },
        "cambium learning group": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "113 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cambium%20Learning"
        },
        "seven senders": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "40 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Seven%20Senders"
        },
        "seven senders gmbh": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "40 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Seven%20Senders"
        },
        "cresta": {
            "score": "4.2/5", "numeric": 4.2, "platform": "Glassdoor", "reviews": "90 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cresta"
        },
        "datarobot": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Glassdoor", "reviews": "576 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=DataRobot"
        },
        "tensorops": {
            "score": "4.4/5", "numeric": 4.4, "platform": "Glassdoor", "reviews": "Glassdoor Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=TensorOps"
        },
        "policyme": {
            "score": "4.3/5", "numeric": 4.3, "platform": "Glassdoor", "reviews": "39 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=PolicyMe"
        },
        "chaingpt": {
            "score": "4.3/5", "numeric": 4.3, "platform": "Glassdoor", "reviews": "Glassdoor Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=ChainGPT"
        },
        "unity": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "2.4k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Unity%20Technologies"
        },
        "scale ai": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Glassdoor", "reviews": "850 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Scale%20AI"
        },
        "tether": {
            "score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "Glassdoor Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Tether%20Operations"
        },
        "tether operations limited": {
            "score": "4.0/5", "numeric": 4.0, "platform": "Glassdoor", "reviews": "Glassdoor Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Tether%20Operations"
        },
        "siemens": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "50k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Siemens"
        },
        "bosch": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "40k+ Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Bosch"
        },
        "outsystems": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Teamlyzer", "reviews": "48 Reviews",
            "url": "https://pt.teamlyzer.com/companies/outsystems"
        },
        "talkdesk": {
            "score": "3.6/5", "numeric": 3.6, "platform": "Teamlyzer", "reviews": "142 Reviews",
            "url": "https://pt.teamlyzer.com/companies/talkdesk"
        },
        "primeit": {
            "score": "2.9/5", "numeric": 2.9, "platform": "Teamlyzer", "reviews": "568 Reviews",
            "url": "https://pt.teamlyzer.com/companies/prime-it"
        }
    }

    # Slugs verificados de empresas em Portugal no Teamlyzer
    TEAMLYZER_SLUGS = {
        "innowave": "innowave-technologies",
        "primeit": "prime-it",
        "prime it": "prime-it",
        "volkswagen": "volkswagen-digital-solutions",
        "volkswagen group": "volkswagen-digital-solutions",
        "feedzai": "feedzai",
        "critical techworks": "critical-techworks",
        "critical software": "critical-software",
        "revolut": "revolut",
        "alten": "alten",
        "nordea": "nordea-asset-management",
        "nordea asset management": "nordea-asset-management",
        "nordea asset management portugal": "nordea-asset-management",
        "outsystems": "outsystems",
        "talkdesk": "talkdesk",
        "celfocus": "celfocus",
        "devoteam": "devoteam",
        "imaginary cloud": "imaginary-cloud",
        "ceiia": "ceiia",
        "unbabel": "unbabel",
        "blip": "blip",
        "farfetch": "farfetch",
        "mindera": "mindera",
        "bold": "bold-by-devoteam",
        "noesis": "noesis"
    }

    @classmethod
    async def fetch_teamlyzer_score(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        comp_clean = company_name.strip()
        comp_lower = comp_clean.lower()
        
        slug = cls.TEAMLYZER_SLUGS.get(comp_lower)
        if not slug:
            for alias, target_slug in cls.TEAMLYZER_SLUGS.items():
                if alias in comp_lower or comp_lower in alias:
                    slug = target_slug
                    break

        if not slug:
            return None

        url = f"https://pt.teamlyzer.com/companies/{slug}"
        try:
            res = await client.get(url, timeout=3.0, follow_redirects=True)
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
        clean_comp = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|operations|portugal)$", "", company_name, flags=re.IGNORECASE).strip()
        clean_comp = clean_comp.replace("&", "and").strip()
        if not clean_comp:
            clean_comp = company_name

        query = f"{clean_comp} glassdoor employee rating reviews"
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        glassdoor_search_url = f"https://www.glassdoor.com/Search/results.htm?keyword={urllib.parse.quote(company_name.strip())}"

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

        comp_clean = company_name.strip()
        comp_lower = comp_clean.lower()
        comp_key = re.sub(r"[^a-z0-9]", "", comp_lower)

        if comp_key in cls._cache:
            return cls._cache[comp_key]

        # 1. Base Verificada Determinística (Instantânea, 100% Precisa e Segura)
        for verified_name, data in cls.VERIFIED_COMPANIES.items():
            if verified_name == comp_lower or verified_name in comp_lower or comp_lower in verified_name:
                cls._cache[comp_key] = data
                return data

        # 2. Teamlyzer Portugal
        teamlyzer_result = await cls.fetch_teamlyzer_score(comp_clean, client)
        if teamlyzer_result:
            cls._cache[comp_key] = teamlyzer_result
            return teamlyzer_result

        # 3. Fallback Dinâmico Glassdoor
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
