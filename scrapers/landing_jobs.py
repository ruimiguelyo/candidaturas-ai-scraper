import json
import logging
from typing import List

import httpx
from bs4 import BeautifulSoup

from models import JobPost
from scrapers.http_utils import ScrapeResults, get_with_retry

logger = logging.getLogger("LandingJobsScraper")


class LandingJobsScraper:
    """Read Landing.jobs' public JSON search response, with an HTML fallback."""

    SEARCH_URL = "https://landing.jobs/jobs/search.json"

    @staticmethod
    def _job_from_offer(item: dict) -> JobPost:
        title = str(item.get("title") or "").strip()
        job_url = str(item.get("url") or "").strip()
        if not title or not job_url:
            raise ValueError("Landing.jobs offer is missing title or URL")

        raw_skills = item.get("skills") or []
        tags = []
        if isinstance(raw_skills, list):
            tags = [
                str(skill.get("name") if isinstance(skill, dict) else skill)
                for skill in raw_skills
                if skill
            ]
        remote = bool(item.get("remote") or item.get("full_remote") or item.get("partial_remote"))
        location = str(item.get("location") or "Unknown")

        return JobPost(
            source="Landing.jobs",
            job_id=str(item.get("id") or item.get("slug") or job_url.rstrip("/").split("/")[-1]),
            title=title,
            company=str(item.get("company_name") or item.get("display_company_name") or "N/A"),
            location=location,
            job_url=job_url,
            modality="Remote" if remote else "On-site / Hybrid",
            is_remote=remote,
            seniority=item.get("experience_level"),
            salary=item.get("salary"),
            post_date=item.get("published_at"),
            tags=tags,
        )

    @staticmethod
    def _html_cards(html: str) -> list[dict]:
        """Fallback for fixtures and any server-rendered cards."""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("article") or soup.find_all("div", class_="job-card")
        offers: list[dict] = []
        for card in cards:
            title_tag = card.select_one("a.job-card__title") or card.find("h2") or card.find("h3")
            link_tag = (
                card.select_one("a.job-card__title[href]")
                or (title_tag if title_tag and title_tag.name == "a" else None)
                or card.find("a", href=True)
            )
            if not title_tag or not link_tag:
                continue
            company_tag = card.select_one(
                ".job-card__company, [class*='company'], [class*='employer']"
            )
            location_tag = card.select_one(
                ".job-card__location, [class*='location'], [class*='where']"
            )
            href = str(link_tag.get("href") or "")
            offers.append(
                {
                    "id": href.rstrip("/").split("/")[-1],
                    "title": title_tag.get_text(" ", strip=True),
                    "company_name": (
                        company_tag.get_text(" ", strip=True)
                        if company_tag
                        else "Landing.jobs Partner"
                    ),
                    "location": (
                        location_tag.get_text(" ", strip=True) if location_tag else "Unknown"
                    ),
                    "url": href if href.startswith("http") else f"https://landing.jobs{href}",
                    "remote": "remote" in card.get_text(" ", strip=True).casefold(),
                }
            )
        return offers

    @classmethod
    def _response_payload(cls, response) -> dict:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("offers"), list):
            return payload

        html = str(getattr(response, "text", "") or "")
        soup = BeautifulSoup(html, "html.parser")
        initial = soup.find("script", id="initial-search-results")
        if initial and initial.string:
            try:
                embedded = json.loads(initial.string)
            except json.JSONDecodeError:
                embedded = None
            if isinstance(embedded, dict) and isinstance(embedded.get("offers"), list):
                return embedded

        cards = cls._html_cards(html)
        if cards:
            return {"offers": cards, "last_page?": False}
        raise RuntimeError("Landing.jobs respondeu sem uma estrutura de vagas reconhecida")

    async def fetch(
        self,
        query: str = "ai",
        limit: int = 30,
        max_pages: int = 3,
    ) -> List[JobPost]:
        if limit <= 0:
            return []

        results: ScrapeResults[JobPost] = ScrapeResults()
        seen_urls: set[str] = set()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,pt;q=0.8",
        }

        async with httpx.AsyncClient(headers=headers, timeout=12) as client:
            for page in range(1, max(1, max_pages) + 1):
                try:
                    response = await get_with_retry(
                        client.get,
                        self.SEARCH_URL,
                        params={"q": query, "page": page},
                    )
                    if response.status_code == 404 and page > 1:
                        break
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Landing.jobs respondeu HTTP {response.status_code} na página {page}"
                        )

                    payload = self._response_payload(response)
                    offers = payload["offers"]
                    if not offers:
                        break

                    new_on_page = 0
                    for item in offers:
                        if len(results) >= limit:
                            break
                        if not isinstance(item, dict):
                            continue
                        try:
                            job = self._job_from_offer(item)
                        except (TypeError, ValueError) as parse_error:
                            logger.debug("Landing.jobs item skipped: %s", parse_error)
                            continue
                        if job.job_url in seen_urls:
                            continue
                        seen_urls.add(job.job_url)
                        results.append(job)
                        new_on_page += 1

                    if (
                        len(results) >= limit
                        or new_on_page == 0
                        or bool(payload.get("last_page?"))
                    ):
                        break
                except Exception as error:
                    logger.error("Landing.jobs error on page %s: %s", page, error)
                    if page > 1:
                        warning = (
                            f"Landing.jobs devolveu resultados parciais: a página {page} falhou "
                            f"depois de {len(results)} vagas."
                        )
                        logger.warning(warning)
                        results.mark_partial(warning)
                        break
                    raise

        return ScrapeResults(
            results[:limit],
            partial=results.partial,
            warning=results.warning,
        )
