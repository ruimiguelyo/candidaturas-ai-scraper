import logging
from typing import List
import httpx
from models import JobPost

logger = logging.getLogger("ArbeitnowScraper")

class ArbeitnowScraper:
    BASE_URL = "https://www.arbeitnow.com/api/job-board-api"

    async def fetch(self, query: str, limit: int = 30) -> List[JobPost]:
        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                res = await client.get(self.BASE_URL, params={"search": query})
                if res.status_code == 200:
                    data = res.json().get("data", [])
                    for item in data[:limit]:
                        results.append(JobPost(
                            source="Arbeitnow",
                            job_id=str(item.get("slug", "")),
                            title=item.get("title", ""),
                            company=item.get("company_name", "N/A"),
                            location=item.get("location", "Remote / Europe"),
                            job_url=item.get("url", ""),
                            modality="Remote" if item.get("remote") else "Hybrid / On-site",
                            is_remote=item.get("remote", True),
                            post_date=item.get("created_at"),
                            tags=item.get("tags", [])
                        ))
            except Exception as e:
                logger.error(f"Arbeitnow error: {e}")
        return results
