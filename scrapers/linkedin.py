import asyncio
import logging
from typing import List, Optional
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from models import JobPost

logger = logging.getLogger("LinkedInScraper")

class LinkedInScraper:
    BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,pt;q=0.8",
        }

    async def fetch(self, search_term: str, location: str, total_wanted: int = 25) -> List[JobPost]:
        results: List[JobPost] = []
        start = 0
        PAGE_SIZE = 25  # LinkedIn Guest API paginates strictly in blocks of 25

        async with AsyncSession(impersonate="chrome120") as session:
            session.headers.update(self.headers)
            while len(results) < total_wanted:
                params = {
                    "keywords": search_term,
                    "location": location,
                    "start": start
                }

                try:
                    res = await session.get(self.BASE_URL, params=params, timeout=self.timeout)

                    if res.status_code == 429:
                        logger.warning("LinkedIn: 429 Too Many Requests (Rate limit atingido). Pausando...")
                        break

                    if res.status_code != 200:
                        logger.warning(f"LinkedIn: Retornou status {res.status_code} na página start={start}")
                        break

                    soup = BeautifulSoup(res.text, "html.parser")
                    cards = soup.find_all("li")

                    if not cards:
                        break

                    for card in cards:
                        if len(results) >= total_wanted:
                            break

                        try:
                            title_tag = card.find("h3", class_="base-search-card__title")
                            company_tag = card.find("h4", class_="base-search-card__subtitle")
                            loc_tag = card.find("span", class_="job-search-card__location")
                            link_tag = card.find("a", class_="base-card__full-link")
                            date_tag = card.find("time")

                            if not (title_tag and link_tag):
                                continue

                            title = title_tag.get_text(strip=True)
                            company = company_tag.get_text(strip=True) if company_tag else "Confidencial"
                            raw_loc = loc_tag.get_text(strip=True) if loc_tag else location
                            job_url = link_tag.get("href", "").split("?")[0]
                            post_date = date_tag.get_text(strip=True) if date_tag else None

                            job_id = job_url.split("-")[-1] if "-" in job_url else job_url

                            is_remote = "remote" in raw_loc.lower() or "remoto" in raw_loc.lower() or "remote" in title.lower()
                            is_hybrid = "hybrid" in raw_loc.lower() or "híbrido" in raw_loc.lower() or "hybrid" in title.lower()

                            modality = "Remote" if is_remote else ("Hybrid" if is_hybrid else "On-site / Hybrid")

                            results.append(JobPost(
                                source="LinkedIn",
                                job_id=str(job_id),
                                title=title,
                                company=company,
                                location=raw_loc,
                                job_url=job_url,
                                modality=modality,
                                is_remote=is_remote,
                                post_date=post_date,
                                seniority=None
                            ))
                        except Exception as card_err:
                            logger.debug(f"LinkedIn item parse skipped: {card_err}")
                            continue

                    start += PAGE_SIZE
                    await asyncio.sleep(0.5)

                except Exception as req_err:
                    logger.error(f"LinkedIn request error at start={start}: {req_err}")
                    break

        return results
