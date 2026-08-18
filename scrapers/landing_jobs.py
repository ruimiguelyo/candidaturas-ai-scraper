import logging
from typing import List
import httpx
from bs4 import BeautifulSoup
from models import JobPost

logger = logging.getLogger("LandingJobsScraper")

class LandingJobsScraper:
    BASE_URL = "https://landing.jobs/jobs"

    async def fetch(self, query: str = "ai", limit: int = 30) -> List[JobPost]:
        results: List[JobPost] = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,pt;q=0.8"
        }
        async with httpx.AsyncClient(headers=headers, timeout=12) as client:
            try:
                params = {"q": query, "page": 1}
                res = await client.get(self.BASE_URL, params=params)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    job_cards = soup.find_all("article") or soup.find_all("div", class_="job-card")
                    for card in job_cards[:limit]:
                        try:
                            title_tag = (
                                card.select_one("a.job-card__title")
                                or card.find("h2")
                                or card.find("h3")
                            )
                            company_tag = card.select_one(
                                ".job-card__company, [class*='company'], [class*='employer']"
                            )
                            if company_tag is title_tag:
                                company_tag = None
                            if company_tag is None:
                                headings = card.find_all(["h2", "h3", "h4"])
                                company_tag = headings[1] if len(headings) > 1 else None
                            link_tag = (
                                card.select_one("a.job-card__title[href]")
                                or (title_tag if title_tag and title_tag.name == "a" else None)
                                or card.find("a", href=True)
                            )

                            if not (title_tag and link_tag):
                                continue

                            title = title_tag.get_text(strip=True)
                            company = company_tag.get_text(strip=True) if company_tag else "Landing.jobs Partner"
                            href = link_tag["href"]
                            full_url = href if href.startswith("http") else f"https://landing.jobs{href}"
                            location_tag = card.select_one(
                                ".job-card__location, [class*='location'], [class*='where']"
                            )
                            location = location_tag.get_text(" ", strip=True) if location_tag else "Unknown"
                            is_remote = "remote" in f"{location} {title}".lower() or "remoto" in f"{location} {title}".lower()

                            results.append(JobPost(
                                source="Landing.jobs",
                                job_id=full_url.split("/")[-1].split("?")[0],
                                title=title,
                                company=company,
                                location=location,
                                job_url=full_url,
                                modality="100% Remote" if is_remote else "Unknown",
                                is_remote=is_remote,
                                seniority=None
                            ))
                        except Exception as parse_err:
                            logger.debug(f"Landing.jobs item skipped: {parse_err}")
                            continue
            except Exception as e:
                logger.error(f"Landing.jobs error: {e}")
        return results
