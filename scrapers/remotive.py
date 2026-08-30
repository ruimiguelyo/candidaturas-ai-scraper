import logging
from typing import List

import httpx

from models import JobPost
from scrapers.http_utils import get_with_retry


logger = logging.getLogger("RemotiveScraper")


class RemotiveScraper:
    """Consume Remotive's documented public job feed and link back to Remotive."""

    BASE_URL = "https://remotive.com/api/remote-jobs"

    async def fetch(self, limit: int = 200) -> List[JobPost]:
        if limit <= 0:
            return []
        async with httpx.AsyncClient(timeout=15) as client:
            response = await get_with_retry(
                client.get,
                self.BASE_URL,
                params={"limit": limit},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Remotive respondeu HTTP {response.status_code}")
            payload = response.json()
            items = payload.get("jobs", [])
            if not isinstance(items, list):
                raise RuntimeError("Remotive devolveu uma lista de vagas inválida")

            jobs: List[JobPost] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
                if not title or not url:
                    continue
                raw_tags = item.get("tags") or []
                tags = raw_tags if isinstance(raw_tags, list) else [str(raw_tags)]
                jobs.append(
                    JobPost(
                        source="Remotive",
                        job_id=str(item.get("id") or url.rstrip("/").split("/")[-1]),
                        title=title,
                        company=str(item.get("company_name") or "N/A"),
                        location=str(item.get("candidate_required_location") or "Remote"),
                        job_url=url,
                        modality="100% Remote",
                        is_remote=True,
                        salary=item.get("salary"),
                        post_date=item.get("publication_date"),
                        seniority=item.get("job_type"),
                        tags=[str(tag) for tag in tags],
                        description_snippet=str(item.get("description") or ""),
                    )
                )
            return jobs
