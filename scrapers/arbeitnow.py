import logging
from typing import List
from datetime import datetime
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
                        raw_date = item.get("created_at")
                        if isinstance(raw_date, (int, float)):
                            try:
                                post_date = datetime.fromtimestamp(raw_date).strftime("%d/%m/%Y")
                            except Exception:
                                post_date = "Recente"
                        else:
                            post_date = str(raw_date) if raw_date else "Recente"

                        results.append(JobPost(
                            source="Arbeitnow",
                            job_id=str(item.get("slug", "")),
                            title=item.get("title", ""),
                            company=item.get("company_name", "N/A"),
                            location=item.get("location", "Remote / Europe"),
                            job_url=item.get("url", ""),
                            modality="Remote" if item.get("remote") else "Hybrid / On-site",
                            is_remote=item.get("remote", True),
                            post_date=post_date,
                            tags=item.get("tags", [])
                        ))
            except Exception as e:
                logger.error(f"Arbeitnow error: {e}")
        return results
