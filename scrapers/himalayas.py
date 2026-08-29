import logging
from typing import List
from datetime import datetime
import httpx
from models import JobPost
from scrapers.http_utils import get_with_retry

logger = logging.getLogger("HimalayasScraper")

class HimalayasScraper:
    BASE_URL = "https://himalayas.app/jobs/api/search"

    async def fetch(self, query: str, limit: int = 30) -> List[JobPost]:
        if limit <= 0:
            return []

        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                res = await get_with_retry(
                    client.get,
                    self.BASE_URL,
                    params={"q": query, "limit": limit},
                )
                if res.status_code != 200:
                    raise RuntimeError(f"Himalayas respondeu HTTP {res.status_code}")
                data = res.json()
                jobs = data.get("jobs", [])
                if not isinstance(jobs, list):
                    raise RuntimeError("Himalayas devolveu uma lista de vagas inválida")
                for item in jobs[:limit]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        title = item.get("title", "")
                        company = item.get("companyName", "N/A")

                        # O URL direto de candidatura no Himalayas vem em 'applicationLink' ou 'guid'
                        url = item.get("applicationLink") or item.get("guid")
                        if not url:
                            company_slug = item.get("companySlug", "")
                            job_slug = item.get("slug", "")
                            if company_slug and job_slug:
                                url = f"https://himalayas.app/companies/{company_slug}/jobs/{job_slug}"
                            else:
                                url = "https://himalayas.app/jobs"

                        min_sal = item.get("minSalary")
                        max_sal = item.get("maxSalary")
                        curr = item.get("currency", "USD")
                        salary_str = (
                            f"{curr} {min_sal:,} - {max_sal:,}"
                            if isinstance(min_sal, (int, float))
                            and isinstance(max_sal, (int, float))
                            else None
                        )

                        restrictions = item.get("locationRestrictions", [])
                        if isinstance(restrictions, str):
                            restrictions = [restrictions]
                        if not isinstance(restrictions, list):
                            restrictions = []
                        loc_desc = (
                            ", ".join(str(value) for value in restrictions if value)
                            or "Worldwide Remote"
                        )

                        raw_pub = item.get("pubDate")
                        if isinstance(raw_pub, (int, float)):
                            try:
                                post_date = datetime.fromtimestamp(raw_pub).strftime("%d/%m/%Y")
                            except Exception:
                                post_date = "Recente"
                        else:
                            post_date = str(raw_pub) if raw_pub else "Recente"
                        raw_categories = item.get("categories", []) or []
                        categories = (
                            raw_categories
                            if isinstance(raw_categories, list)
                            else [str(raw_categories)]
                        )

                        results.append(
                            JobPost(
                                source="Himalayas",
                                job_id=str(item.get("id", url.split("/")[-1])),
                                title=title,
                                company=company,
                                location=loc_desc,
                                job_url=url,
                                modality="100% Remote",
                                is_remote=True,
                                salary=salary_str,
                                post_date=post_date,
                                seniority=item.get("experienceLevel") or item.get("seniority"),
                                tags=categories,
                                description_snippet=item.get("excerpt", ""),
                            )
                        )
                    except Exception as parse_error:
                        logger.debug("Himalayas item skipped: %s", parse_error)
            except Exception as e:
                logger.error(f"Himalayas error: {e}")
                raise
        return results
