import logging
from typing import List
import httpx
from models import JobPost

logger = logging.getLogger("JobicyScraper")

class JobicyScraper:
    BASE_URL = "https://jobicy.com/api/v2/remote-jobs"

    async def fetch(self, tag: str = "ai", count: int = 30) -> List[JobPost]:
        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                res = await client.get(self.BASE_URL, params={"tag": tag, "count": count})
                if res.status_code == 200:
                    data = res.json()
                    jobs = data.get("jobs", [])
                    for item in jobs:
                        level = item.get("jobLevel", "")
                        results.append(JobPost(
                            source="Jobicy",
                            job_id=str(item.get("id", "")),
                            title=item.get("jobTitle", ""),
                            company=item.get("companyName", "N/A"),
                            location=item.get("jobGeo", "100% Remote"),
                            job_url=item.get("url", ""),
                            modality="100% Remote",
                            is_remote=True,
                            seniority="Junior / Trainee / Internship" if "entry" in level.lower() or "junior" in level.lower() else level,
                            post_date=item.get("pubDate")
                        ))
            except Exception as e:
                logger.error(f"Jobicy error: {e}")
        return results
