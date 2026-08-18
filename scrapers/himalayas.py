import logging
from typing import List
from datetime import datetime
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

                        # Formatação do Salário
                        min_sal = item.get("minSalary")
                        max_sal = item.get("maxSalary")
                        curr = item.get("currency", "USD")
                        salary_str = f"{curr} {min_sal:,} - {max_sal:,}" if (
                            isinstance(min_sal, (int, float)) and isinstance(max_sal, (int, float))
                        ) else None

                        # Localização / Restrições
                        restrictions = item.get("locationRestrictions", [])
                        if isinstance(restrictions, str):
                            restrictions = [restrictions]
                        if not isinstance(restrictions, list):
                            restrictions = []
                        loc_desc = ", ".join(str(value) for value in restrictions if value) or "Worldwide Remote"

                        # Formatação de Data (Unix Timestamp -> DD/MM/YYYY)
                        raw_pub = item.get("pubDate")
                        if isinstance(raw_pub, (int, float)):
                            try:
                                post_date = datetime.fromtimestamp(raw_pub).strftime("%d/%m/%Y")
                            except Exception:
                                post_date = "Recente"
                        else:
                            post_date = str(raw_pub) if raw_pub else "Recente"

                        results.append(JobPost(
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
                            description_snippet=item.get("excerpt", "")
                        ))
            except Exception as e:
                logger.error(f"Himalayas error: {e}")
        return results
