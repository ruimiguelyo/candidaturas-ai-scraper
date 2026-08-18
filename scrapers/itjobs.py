import logging
from typing import List
import xml.etree.ElementTree as ET
import httpx
from models import JobPost

logger = logging.getLogger("ITJobsScraper")

class ITJobsScraper:
    FEED_URL = "https://www.itjobs.pt/emprego/feed"

    async def fetch(self, query: str = "ai", limit: int = 40) -> List[JobPost]:
        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                res = await client.get(self.FEED_URL)
                if res.status_code == 200:
                    root = ET.fromstring(res.content)
                    items = root.findall(".//item")
                    for item in items:
                        title = item.findtext("title", "")
                        desc = item.findtext("description", "")
                        link = item.findtext("link", "")
                        guid = item.findtext("guid", link)
                        pub_date = item.findtext("pubDate", "")

                        # Verificação de texto relevante
                        combined_text = f"{title} {desc}".lower()
                        
                        is_remote = "remoto" in combined_text or "remote" in combined_text
                        is_hybrid = "híbrido" in combined_text or "hybrid" in combined_text

                        modality = "Remote" if is_remote else ("Hybrid" if is_hybrid else "Portugal (Híbrido/Presencial)")

                        results.append(JobPost(
                            source="ITJobs.pt",
                            job_id=guid.split("/")[-1] if "/" in guid else guid,
                            title=title,
                            company="Consultar Anúncio",
                            location="Portugal (Lisboa / Porto / Remoto)",
                            job_url=link,
                            modality=modality,
                            is_remote=is_remote,
                            post_date=pub_date,
                            description_snippet=desc[:300] if desc else None
                        ))
                        if len(results) >= limit:
                            break
            except Exception as e:
                logger.error(f"ITJobs.pt error: {e}")
        return results
