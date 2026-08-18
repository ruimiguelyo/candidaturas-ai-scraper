import logging
from typing import List
import httpx
from models import JobPost

logger = logging.getLogger("HimalayasScraper")

class HimalayasScraper:
    BASE_URL = "https://himalayas.app/jobs/api/search"

    async def fetch(self, query: str, limit: int = 30) -> List[JobPost]:
        results: List[JobPost] = []
        async with httpx.AsyncClient(timeout=12) as client:
            try:
                res = await client.get(self.BASE_URL, params={"q": query, "limit": limit})
                if res.status_code == 200:
                    data = res.json()
                    jobs = data.get("jobs", [])
                    for item in jobs:
                        slug = item.get("slug", "")
                        title = item.get("title", "")
                        company = item.get("companyName", "N/A")
                        url = item.get("applicationUrl") or f"https://himalayas.app/jobs/{slug}"
                        
                        min_sal = item.get("minSalary")
                        max_sal = item.get("maxSalary")
                        curr = item.get("currency", "USD")
                        salary_str = f"{curr} {min_sal:,} - {max_sal:,}" if (min_sal and max_sal) else None

                        restrictions = item.get("locationRestrictions", [])
                        loc_desc = ", ".join(restrictions) if restrictions else "Worldwide Remote"

                        results.append(JobPost(
                            source="Himalayas",
                            job_id=str(item.get("id", slug)),
                            title=title,
                            company=company,
                            location=loc_desc,
                            job_url=url,
                            modality="100% Remote",
                            is_remote=True,
                            salary=salary_str,
                            post_date=item.get("pubDate"),
                            seniority=item.get("seniority", "Junior / Entry"),
                            description_snippet=item.get("excerpt", "")
                        ))
            except Exception as e:
                logger.error(f"Himalayas error: {e}")
        return results
