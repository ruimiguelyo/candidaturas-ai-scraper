import logging
from typing import List
import httpx
from models import JobPost

logger = logging.getLogger("RemoteOKScraper")

class RemoteOKScraper:
    BASE_URL = "https://remoteok.com/api"

    async def fetch(self, query: str, limit: int = 30) -> List[JobPost]:
        results: List[JobPost] = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AIJobFinder/1.0"}
        async with httpx.AsyncClient(headers=headers, timeout=12) as client:
            try:
                res = await client.get(self.BASE_URL, params={"tag": query})
                if res.status_code == 200:
                    data = res.json()
                    valid_items = [i for i in data if isinstance(i, dict) and "id" in i]
                    for item in valid_items[:limit]:
                        results.append(JobPost(
                            source="RemoteOK",
                            job_id=str(item.get("id")),
                            title=item.get("position", ""),
                            company=item.get("company", "N/A"),
                            location="100% Remote",
                            job_url=item.get("url", ""),
                            modality="100% Remote",
                            is_remote=True,
                            tags=item.get("tags", []),
                            post_date=item.get("date")
                        ))
            except Exception as e:
                logger.error(f"RemoteOK error: {e}")
        return results
