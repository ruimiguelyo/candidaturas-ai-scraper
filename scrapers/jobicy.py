import logging
from typing import List
import httpx
from models import JobPost
from scrapers.http_utils import get_with_retry

logger = logging.getLogger("JobicyScraper")

class JobicyScraper:
    BASE_URL = "https://jobicy.com/api/v2/remote-jobs"

    async def fetch(self, tag: str | None = "ai", count: int = 30) -> List[JobPost]:
        if count <= 0:
            return []

        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                params = {"count": count}
                if tag:
                    params["tag"] = tag
                res = await get_with_retry(client.get, self.BASE_URL, params=params)
                if res.status_code != 200:
                    raise RuntimeError(f"Jobicy respondeu HTTP {res.status_code}")
                data = res.json()
                jobs = data.get("jobs", [])
                if not isinstance(jobs, list):
                    raise RuntimeError("Jobicy devolveu uma lista de vagas inválida")
                for item in jobs[:count]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        level = str(item.get("jobLevel", "") or "")
                        title = item.get("jobTitle", "")
                        job_url = item.get("url", "")
                        if not title or not job_url:
                            continue
                        raw_tags = item.get("jobTags", []) or item.get("tags", []) or []
                        tags = raw_tags if isinstance(raw_tags, list) else [str(raw_tags)]
                        results.append(
                            JobPost(
                                source="Jobicy",
                                job_id=str(item.get("id", "")),
                                title=title,
                                company=item.get("companyName", "N/A"),
                                location=item.get("jobGeo", "100% Remote"),
                                job_url=job_url,
                                modality="100% Remote",
                                is_remote=True,
                                seniority=level,
                                post_date=item.get("pubDate"),
                                tags=tags,
                                description_snippet=item.get("jobDescription", "")
                                or item.get("description", ""),
                            )
                        )
                    except Exception as parse_error:
                        logger.debug("Jobicy item skipped: %s", parse_error)
            except Exception as e:
                logger.error(f"Jobicy error: {e}")
                raise
        return results
