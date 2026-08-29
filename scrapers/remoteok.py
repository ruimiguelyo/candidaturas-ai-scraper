import logging
from typing import List
import httpx
from models import JobPost
from scrapers.http_utils import get_with_retry

logger = logging.getLogger("RemoteOKScraper")

class RemoteOKScraper:
    BASE_URL = "https://remoteok.com/api"

    async def fetch(self, query: str | None = None, limit: int = 30) -> List[JobPost]:
        if limit <= 0:
            return []

        results: List[JobPost] = []
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AIJobFinder/1.0"}
        async with httpx.AsyncClient(headers=headers, timeout=12) as client:
            try:
                params = {"tag": query} if query else {}
                res = await get_with_retry(client.get, self.BASE_URL, params=params)
                if res.status_code != 200:
                    raise RuntimeError(f"RemoteOK respondeu HTTP {res.status_code}")
                data = res.json()
                if not isinstance(data, list):
                    raise RuntimeError("RemoteOK devolveu uma lista de vagas inválida")
                valid_items = [i for i in data if isinstance(i, dict) and "id" in i]
                for item in valid_items:
                    if len(results) >= limit:
                        break
                    try:
                        title = item.get("position", "")
                        job_url = item.get("url", "")
                        if not title or not job_url:
                            continue
                        raw_tags = item.get("tags", []) or []
                        tags = raw_tags if isinstance(raw_tags, list) else [str(raw_tags)]
                        results.append(
                            JobPost(
                                source="RemoteOK",
                                job_id=str(item.get("id")),
                                title=title,
                                company=item.get("company", "N/A"),
                                location=item.get("location") or "100% Remote",
                                job_url=job_url,
                                modality="100% Remote",
                                is_remote=True,
                                tags=tags,
                                post_date=item.get("date"),
                                description_snippet=item.get("description", ""),
                            )
                        )
                    except Exception as parse_error:
                        logger.debug("RemoteOK item skipped: %s", parse_error)
            except Exception as e:
                logger.error(f"RemoteOK error: {e}")
                raise
        return results
