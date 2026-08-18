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
    Motor de Ranking com Prioridade Estrita:
    REGRA 1: SE A EMPRESA EXISTE NO TEAMLYZER PORTUGAL -> USA OBRIGATORIAMENTE O TEAMLYZER.
    REGRA 2: APENAS SE NÃO EXISTIR NO TEAMLYZER (EMPRESAS ESTRANGEIRAS/US) -> FAZ FALLBACK PARA O GLASSDOOR.
    """

    _cache: Dict[str, Optional[Dict[str, any]]] = {}

    # 1. Base Verificada de Empresas no TEAMLYZER PORTUGAL (PRIORIDADE ABSOLUTA #1)
    VERIFIED_TEAMLYZER_PT = {
        "fujitsu": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "110 Reviews",
            "url": "https://pt.teamlyzer.com/companies/fujitsu"
        },
        "fujitsu portugal": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "110 Reviews",
            "url": "https://pt.teamlyzer.com/companies/fujitsu"
        },
        "innowave": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Teamlyzer", "reviews": "147 Reviews",
            "url": "https://pt.teamlyzer.com/companies/innowave-technologies"
        },
        "innowave technologies": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Teamlyzer", "reviews": "147 Reviews",
            "url": "https://pt.teamlyzer.com/companies/innowave-technologies"
        },
        "volkswagen": {
            "score": "3.5/5", "numeric": 3.5, "platform": "Teamlyzer", "reviews": "144 Reviews",
            "url": "https://pt.teamlyzer.com/companies/volkswagen-digital-solutions"
        },
        "volkswagen group": {
            "score": "3.5/5", "numeric": 3.5, "platform": "Teamlyzer", "reviews": "144 Reviews",
            "url": "https://pt.teamlyzer.com/companies/volkswagen-digital-solutions"
        },
        "critical techworks": {
            "score": "3.0/5", "numeric": 3.0, "platform": "Teamlyzer", "reviews": "641 Reviews",
            "url": "https://pt.teamlyzer.com/companies/critical-techworks"
        },
        "critical software": {
            "score": "3.7/5", "numeric": 3.7, "platform": "Teamlyzer", "reviews": "120 Reviews",
            "url": "https://pt.teamlyzer.com/companies/critical-software"
        },
        "feedzai": {
            "score": "3.4/5", "numeric": 3.4, "platform": "Teamlyzer", "reviews": "121 Reviews",
            "url": "https://pt.teamlyzer.com/companies/feedzai"
        },
        "revolut": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "54 Reviews",
            "url": "https://pt.teamlyzer.com/companies/revolut"
        },
        "santander": {
            "score": "3.2/5", "numeric": 3.2, "platform": "Teamlyzer", "reviews": "45 Reviews",
            "url": "https://pt.teamlyzer.com/companies/santander-totta"
        },
        "banco santander": {
            "score": "3.2/5", "numeric": 3.2, "platform": "Teamlyzer", "reviews": "45 Reviews",
            "url": "https://pt.teamlyzer.com/companies/santander-totta"
        },
        "nordea": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "1 Review",
            "url": "https://pt.teamlyzer.com/companies/nordea-asset-management"
        },
        "nordea asset management": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "1 Review",
            "url": "https://pt.teamlyzer.com/companies/nordea-asset-management"
        },
        "nordea asset management portugal": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "1 Review",
            "url": "https://pt.teamlyzer.com/companies/nordea-asset-management"
        },
        "mckinsey": {
            "score": "3.3/5", "numeric": 3.3, "platform": "Teamlyzer", "reviews": "17 Reviews",
            "url": "https://pt.teamlyzer.com/companies/mckinsey-company"
        },
        "mckinsey & company": {
            "score": "3.3/5", "numeric": 3.3, "platform": "Teamlyzer", "reviews": "17 Reviews",
            "url": "https://pt.teamlyzer.com/companies/mckinsey-company"
        },
        "havi": {
            "score": "3.6/5", "numeric": 3.6, "platform": "Teamlyzer", "reviews": "8 Reviews",
            "url": "https://pt.teamlyzer.com/companies/havi-portugal"
        },
        "havi techhub": {
            "score": "3.6/5", "numeric": 3.6, "platform": "Teamlyzer", "reviews": "8 Reviews",
            "url": "https://pt.teamlyzer.com/companies/havi-portugal"
        },
        "philip morris": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "15 Reviews",
            "url": "https://pt.teamlyzer.com/companies/tabaqueira"
        },
        "philip morris international": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "15 Reviews",
            "url": "https://pt.teamlyzer.com/companies/tabaqueira"
        },
        "alten": {
            "score": "2.3/5", "numeric": 2.3, "platform": "Teamlyzer", "reviews": "20 Reviews",
            "url": "https://pt.teamlyzer.com/companies/alten"
        },
        "ceiia": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "18 Reviews",
            "url": "https://pt.teamlyzer.com/companies/ceiia"
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
        },
        "siemens": {
            "score": "3.6/5", "numeric": 3.6, "platform": "Teamlyzer", "reviews": "65 Reviews",
            "url": "https://pt.teamlyzer.com/companies/siemens-sa"
        },
        "bosch": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "110 Reviews",
            "url": "https://pt.teamlyzer.com/companies/bosch-portugal"
        },
        "celfocus": {
            "score": "3.3/5", "numeric": 3.3, "platform": "Teamlyzer", "reviews": "85 Reviews",
            "url": "https://pt.teamlyzer.com/companies/celfocus"
        },
        "unbabel": {
            "score": "3.1/5", "numeric": 3.1, "platform": "Teamlyzer", "reviews": "34 Reviews",
            "url": "https://pt.teamlyzer.com/companies/unbabel"
        },
        "mindera": {
            "score": "3.8/5", "numeric": 3.8, "platform": "Teamlyzer", "reviews": "95 Reviews",
            "url": "https://pt.teamlyzer.com/companies/mindera"
        },
        "blip": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Teamlyzer", "reviews": "115 Reviews",
            "url": "https://pt.teamlyzer.com/companies/blip"
        },
        "farfetch": {
            "score": "3.4/5", "numeric": 3.4, "platform": "Teamlyzer", "reviews": "340 Reviews",
            "url": "https://pt.teamlyzer.com/companies/farfetch"
        },
        "bold": {
            "score": "2.7/5", "numeric": 2.7, "platform": "Teamlyzer", "reviews": "90 Reviews",
            "url": "https://pt.teamlyzer.com/companies/bold-by-devoteam"
        },
        "noesis": {
            "score": "3.0/5", "numeric": 3.0, "platform": "Teamlyzer", "reviews": "180 Reviews",
            "url": "https://pt.teamlyzer.com/companies/noesis"
        }
    }

    # 2. Base de Empresas Internacionais / Remotas sem perfil no Teamlyzer (FALLBACK GLASSDOOR)
    VERIFIED_GLASSDOOR_GLOBAL = {
        "cloudflare": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "3.5k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Cloudflare"
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
        "euronext": {
            "score": "3.9/5", "numeric": 3.9, "platform": "Glassdoor", "reviews": "650 Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Euronext"
        },
        "zebra technologies": {
            "score": "4.1/5", "numeric": 4.1, "platform": "Glassdoor", "reviews": "3.8k Reviews",
            "url": "https://www.glassdoor.com/Search/results.htm?keyword=Zebra%20Technologies"
        }
    }

    @classmethod
    async def fetch_dynamic_teamlyzer(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        """Tenta buscar no Teamlyzer calculando o slug dinamicamente."""
        clean = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|portugal)$", "", company_name, flags=re.IGNORECASE).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
        if not slug:
            return None

        for test_slug in [slug, f"{slug}-portugal", f"{slug}-technologies"]:
            url = f"https://pt.teamlyzer.com/companies/{test_slug}"
            try:
                res = await client.get(url, timeout=2.5, follow_redirects=True)
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
    async def fetch_dynamic_glassdoor(cls, company_name: str, client: httpx.AsyncClient) -> Optional[Dict[str, any]]:
        clean_comp = re.sub(r"(?:gmbh|inc\.?|llc|ltd|limited|corporation|group|operations|portugal)$", "", company_name, flags=re.IGNORECASE).strip()
        clean_comp = clean_comp.replace("&", "and").strip()
        if not clean_comp:
            clean_comp = company_name

        query = f"{clean_comp} glassdoor employee rating reviews"
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        glassdoor_search_url = f"https://www.glassdoor.com/Search/results.htm?keyword={urllib.parse.quote(company_name.strip())}"

        try:
            res = await client.get(url, timeout=3.0)
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

        # =========================================================================
        # PASSO 1: PRIORIDADE ABSOLUTA AO TEAMLYZER PORTUGAL
        # =========================================================================
        # 1.1 Base Verificada do Teamlyzer (Fujitsu, InnoWave, Volkswagen, Santander, etc.)
        for verified_name, data in cls.VERIFIED_TEAMLYZER_PT.items():
            if verified_name == comp_lower or verified_name in comp_lower or comp_lower in verified_name:
                cls._cache[comp_key] = data
                return data

        # 1.2 Busca Dinâmica no Teamlyzer
        dynamic_teamlyzer = await cls.fetch_dynamic_teamlyzer(comp_clean, client)
        if dynamic_teamlyzer:
            cls._cache[comp_key] = dynamic_teamlyzer
            return dynamic_teamlyzer

        # =========================================================================
        # PASSO 2: FALLBACK PARA GLASSDOOR (APENAS SE NÃO EXISTIR NO TEAMLYZER)
        # =========================================================================
        # 2.1 Base Verificada de Gigantes Globais / US (Cloudflare, Google, Stripe, etc.)
        for global_name, data in cls.VERIFIED_GLASSDOOR_GLOBAL.items():
            if global_name == comp_lower or global_name in comp_lower or comp_lower in global_name:
                cls._cache[comp_key] = data
                return data

        # 2.2 Busca Dinâmica no Glassdoor
        dynamic_glassdoor = await cls.fetch_dynamic_glassdoor(comp_clean, client)
        if dynamic_glassdoor:
            cls._cache[comp_key] = dynamic_glassdoor
            return dynamic_glassdoor

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
