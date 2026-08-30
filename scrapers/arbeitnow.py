import logging
import re
from datetime import datetime
from typing import List
from urllib.parse import urljoin

import httpx

from models import JobPost
from scrapers.http_utils import ScrapeResults, get_with_retry

logger = logging.getLogger("ArbeitnowScraper")


class ArbeitnowScraper:
    BASE_URL = "https://www.arbeitnow.com/api/job-board-api"

    async def fetch(
        self,
        query: str | None = None,
        limit: int = 30,
        max_pages: int = 5,
    ) -> List[JobPost]:
        if limit <= 0:
            return []

        results: ScrapeResults[JobPost] = ScrapeResults()
        seen_keys: set[str] = set()
        query_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(query or "").casefold())
            if len(token) >= 3
        }
        next_url: str | None = self.BASE_URL
        next_params: dict[str, object] | None = {"page": 1}
        if query:
            next_params["search"] = query

        async with httpx.AsyncClient(timeout=12) as client:
            for page_number in range(1, max(1, max_pages) + 1):
                if not next_url:
                    break

                try:
                    response = await get_with_retry(client.get, next_url, params=next_params)
                    if response.status_code == 404 and page_number > 1:
                        break
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Arbeitnow respondeu HTTP {response.status_code} na página {page_number}"
                        )

                    payload = response.json()
                    data = payload.get("data", [])
                    if not isinstance(data, list):
                        raise RuntimeError("Arbeitnow devolveu uma lista de vagas inválida")
                    if not data:
                        break

                    # Match the complete page before applying the requested result cap.
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        try:
                            searchable = " ".join(
                                str(item.get(field, ""))
                                for field in ("title", "description", "job_description", "tags")
                            ).casefold()
                            searchable_tokens = set(re.findall(r"[a-z0-9]+", searchable))
                            if query_tokens and not query_tokens.intersection(searchable_tokens):
                                continue

                            identity = str(item.get("url") or item.get("slug") or "").strip()
                            if not identity:
                                identity = (
                                    f"{item.get('company_name', '')}:{item.get('title', '')}".casefold()
                                )
                            if identity in seen_keys:
                                continue

                            raw_remote = item.get("remote", False)
                            is_remote = raw_remote is True or str(raw_remote).strip().lower() in {
                                "1",
                                "true",
                                "yes",
                                "remote",
                            }
                            raw_date = item.get("created_at")
                            if isinstance(raw_date, (int, float)):
                                try:
                                    post_date = datetime.fromtimestamp(raw_date).strftime("%d/%m/%Y")
                                except Exception:
                                    post_date = None
                            else:
                                post_date = str(raw_date) if raw_date else None

                            raw_tags = item.get("tags", []) or []
                            tags = raw_tags if isinstance(raw_tags, list) else [str(raw_tags)]

                            job = JobPost(
                                source="Arbeitnow",
                                job_id=str(item.get("slug", "")),
                                title=item.get("title", ""),
                                company=item.get("company_name", "N/A"),
                                location=item.get("location", "Remote / Europe"),
                                job_url=item.get("url", ""),
                                modality="Remote" if is_remote else "Hybrid / On-site",
                                is_remote=is_remote,
                                post_date=post_date,
                                tags=tags,
                                description_snippet=item.get("description", "")
                                or item.get("job_description", ""),
                                discovery_query=query,
                            )
                            seen_keys.add(identity)
                            results.append(job)
                        except Exception as parse_error:
                            logger.debug("Arbeitnow item skipped: %s", parse_error)
                        if len(results) >= limit:
                            break

                    if len(results) >= limit:
                        break

                    next_url, next_params = self._next_page_request(payload, query)
                except Exception as error:
                    logger.error("Arbeitnow error on page %s: %s", page_number, error)
                    if page_number > 1:
                        warning = (
                            f"Arbeitnow devolveu resultados parciais: a página {page_number} falhou "
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

    @classmethod
    def _next_page_request(
        cls,
        payload: dict,
        query: str | None,
    ) -> tuple[str | None, dict[str, object] | None]:
        next_url = payload.get("next_page_url")
        links = payload.get("links")
        if not next_url and isinstance(links, dict):
            next_url = links.get("next")

        meta = payload.get("meta")
        if not next_url and isinstance(meta, dict):
            next_url = meta.get("next_page_url")
        if next_url:
            return urljoin(cls.BASE_URL, str(next_url)), None

        if isinstance(meta, dict):
            try:
                current_page = int(meta.get("current_page"))
                last_page = int(meta.get("last_page"))
            except (TypeError, ValueError):
                return None, None
            if current_page < last_page:
                params: dict[str, object] = {"page": current_page + 1}
                if query:
                    params["search"] = query
                return cls.BASE_URL, params

        return None, None
