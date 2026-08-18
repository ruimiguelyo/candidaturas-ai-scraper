import asyncio
import logging
import re
import urllib.parse
from typing import List, Optional
import bs4
from curl_cffi.requests import AsyncSession
from models import JobPost

logger = logging.getLogger("ITJobsScraper")

class ITJobsScraper:
    """Scraper robusto e de alta precisão para o portal de emprego português ITJobs.pt."""
    
    BASE_URL = "https://www.itjobs.pt"
    SEARCH_URL = "https://www.itjobs.pt/emprego"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

    async def fetch(self, search_term: str, location: Optional[str] = None, max_pages: int = 2) -> List[JobPost]:
        results: List[JobPost] = []
        seen_ids = set()

        async with AsyncSession(impersonate="chrome120") as session:
            session.headers.update(self.headers)
            
            for page in range(1, max_pages + 1):
                params = {"q": search_term}
                if location:
                    params["location"] = location
                if page > 1:
                    params["page"] = str(page)

                try:
                    res = await session.get(self.SEARCH_URL, params=params, timeout=self.timeout)
                    if res.status_code != 200:
                        logger.warning(f"ITJobs retornou status {res.status_code} para '{search_term}' na pág {page}")
                        break

                    soup = bs4.BeautifulSoup(res.text, "html.parser")
                    
                    # Procura todos os links de ofertas de emprego (ignora /curso/)
                    offer_links = soup.find_all("a", class_="title", href=lambda h: h and "/oferta/" in h)
                    if not offer_links:
                        offer_links = soup.find_all("a", href=lambda h: h and "/oferta/" in h)

                    for link in offer_links:
                        href = link.get("href", "")
                        # Exemplo: /oferta/515321/junior-ai-engineer
                        parts = href.strip("/").split("/")
                        if len(parts) >= 2 and parts[1].isdigit():
                            job_id = parts[1]
                        else:
                            job_id = re.sub(r"[^a-z0-9]", "", href)

                        if not job_id or job_id in seen_ids:
                            continue
                        seen_ids.add(job_id)

                        raw_title = link.get_text(" ", strip=True).replace("\xa0", " ")
                        if not raw_title or len(raw_title) < 3:
                            continue

                        # Subir para o bloco pai do anúncio
                        parent = (
                            link.find_parent("div", class_="details")
                            or link.find_parent("div", class_="content")
                            or link.find_parent("article")
                            or link.find_parent("li")
                            or link.find_parent("div")
                        )

                        comp_name = "Consultar Anúncio"
                        loc_name = "Portugal"
                        date_str = None
                        salary = None
                        is_remote = False

                        if parent:
                            # 1. Extração do Nome da Empresa
                            comp_el = parent.find("a", href=lambda h: h and "/empresa/" in h)
                            if comp_el:
                                if comp_el.get("title"):
                                    comp_name = comp_el["title"].strip()
                                elif comp_el.find("img") and comp_el.find("img").get("alt"):
                                    comp_name = comp_el.find("img")["alt"].strip()
                                elif comp_el.get_text(strip=True):
                                    comp_name = comp_el.get_text(strip=True)

                            # 2. Extração de Data, Localização e Salário no texto
                            parent_text = parent.get_text(" ", strip=True).replace("\xa0", " ")
                            
                            if "remoto" in parent_text.lower() or "remote" in parent_text.lower():
                                is_remote = True
                                modality = "100% Remote"
                            elif "híbrido" in parent_text.lower() or "hibrido" in parent_text.lower():
                                modality = "On-site / Hybrid"
                            else:
                                modality = "On-site / Hybrid"

                            if "lisboa" in parent_text.lower() or "lisbon" in parent_text.lower():
                                loc_name = "Lisbon, Portugal"
                            elif "porto" in parent_text.lower():
                                loc_name = "Porto, Portugal"
                            elif "coimbra" in parent_text.lower():
                                loc_name = "Coimbra, Portugal"
                            elif "braga" in parent_text.lower():
                                loc_name = "Braga, Portugal"
                            elif "aveiro" in parent_text.lower():
                                loc_name = "Aveiro, Portugal"
                            elif is_remote:
                                loc_name = "Portugal (Remote)"

                            # Data (ex: "14 ago", "23 jul", "Hoje")
                            date_match = re.search(r"(\d{1,2}\s+(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez))", parent_text, re.IGNORECASE)
                            if date_match:
                                date_str = date_match.group(1)

                            # Salário (ex: €20 000 - €45 000)
                            sal_match = re.search(r"([€$]\s*[\d\s]+(?:-\s*[€$]?\s*[\d\s]+)?)", parent_text)
                            if sal_match:
                                salary = sal_match.group(1).strip()

                        full_url = f"{self.BASE_URL}{href}" if href.startswith("/") else href

                        results.append(JobPost(
                            source="ITJobs.pt",
                            job_id=job_id,
                            title=raw_title,
                            company=comp_name,
                            location=loc_name,
                            job_url=full_url,
                            modality=modality,
                            is_remote=is_remote,
                            salary=salary,
                            post_date=date_str,
                            description_snippet=None
                        ))

                except Exception as e:
                    logger.error(f"Erro ao raspar ITJobs.pt para '{search_term}': {e}")
                    break

        logger.info(f"ITJobs.pt: '{search_term}' retornou {len(results)} ofertas.")
        return results
