"""Utilitários HTTP partilhados, cache ETag, limpeza de HTML e estrutura de resposta dos scrapers."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import bs4

from estagio_radar.models import RawJob, SourceHealth

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

RETRYABLE_STATUS_CODES = {408, 425, 500, 502, 503, 504}


@dataclass
class ScraperBatchResult:
    jobs: list[RawJob] = field(default_factory=list)
    health: SourceHealth = field(default_factory=lambda: SourceHealth(source="unknown"))


class ETagCache:
    """Cache em memória e disco de headers ETag/Last-Modified e RawJobs para Turbo-Sniper."""

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._entries: dict[str, tuple[str | None, str | None, list[RawJob]]] = {}
        self.persist_path: Path | None = Path(persist_path) if persist_path else None
        self._dirty = False
        if self.persist_path and self.persist_path.exists():
            self._load()

    def _load(self) -> None:
        if not self.persist_path:
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for url, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                jobs = [RawJob.model_validate(j) for j in (entry.get("jobs") or []) if isinstance(j, dict)]
                self._entries[str(url)] = (
                    entry.get("etag"),
                    entry.get("last_mod"),
                    jobs,
                )
        except Exception:
            self._entries.clear()

    def save(self) -> None:
        if not self.persist_path or not self._dirty:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                url: {
                    "etag": etag,
                    "last_mod": last_mod,
                    "jobs": [j.model_dump(mode="json") for j in jobs],
                }
                for url, (etag, last_mod, jobs) in self._entries.items()
            }
            tmp_path = self.persist_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self.persist_path)
            self._dirty = False
        except Exception:
            pass

    def request_headers(self, url: str) -> dict[str, str]:
        entry = self._entries.get(url)
        if not entry:
            return {}
        etag, last_mod, _ = entry
        hdrs: dict[str, str] = {}
        if etag:
            hdrs["If-None-Match"] = etag
        if last_mod:
            hdrs["If-Modified-Since"] = last_mod
        return hdrs

    def get_cached_jobs(self, url: str) -> list[RawJob]:
        entry = self._entries.get(url)
        return list(entry[2]) if entry else []

    def store(self, url: str, headers: Any, jobs: list[RawJob]) -> None:
        etag = headers.get("etag") or headers.get("ETag") if headers else None
        last_mod = headers.get("last-modified") or headers.get("Last-Modified") if headers else None
        if etag or last_mod:
            self._entries[url] = (str(etag) if etag else None, str(last_mod) if last_mod else None, list(jobs))
            self._dirty = True


class DomainCircuitBreaker:
    """Protege contra HTTP 429 repetidos aplicando cooldown exponencial por domínio."""

    def __init__(self, *, max_cooldown: float = 900.0) -> None:
        self._max_cooldown = max_cooldown
        self._state: dict[str, tuple[float, int]] = {}  # domain -> (open_until_mono, strikes)

    def is_open(self, domain: str) -> bool:
        entry = self._state.get(domain.casefold())
        if not entry:
            return False
        open_until, _strikes = entry
        return time.monotonic() < open_until

    def remaining_seconds(self, domain: str) -> int:
        entry = self._state.get(domain.casefold())
        if not entry:
            return 0
        return max(0, int(entry[0] - time.monotonic()))

    def record_rate_limit(self, domain: str, *, base_cooldown: float = 120.0) -> float:
        key = domain.casefold()
        _prev_until, prev_strikes = self._state.get(key, (0.0, 0))
        strikes = prev_strikes + 1
        cooldown = min(self._max_cooldown, base_cooldown * (2 ** (strikes - 1)))
        self._state[key] = (time.monotonic() + cooldown, strikes)
        return cooldown

    def record_success(self, domain: str) -> None:
        self._state.pop(domain.casefold(), None)


async def retry_async(
    request_fn: Callable[..., Awaitable[Any]],
    *args: Any,
    retries: int = 2,
    base_delay: float = 0.8,
    **kwargs: Any,
) -> Any:
    """Executa pedido HTTP assíncrono com backoff exponencial em erros transitórios."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await request_fn(*args, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            if status in RETRYABLE_STATUS_CODES and attempt < retries:
                await asyncio.sleep(base_delay * (2**attempt))
                continue
            return response
        except Exception as err:
            last_exc = err
            if attempt >= retries:
                raise
            await asyncio.sleep(base_delay * (2**attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("Falha inesperada em retry_async")


def clean_html_text(raw_html: str | None, max_len: int = 1500) -> str:
    """Converte HTML ou texto escapado em texto limpo compacto."""
    if not raw_html:
        return ""
    unescaped = html_lib.unescape(str(raw_html))
    if "<" in unescaped and ">" in unescaped:
        soup = bs4.BeautifulSoup(unescaped, "lxml")
        text = soup.get_text(" ", strip=True)
    else:
        text = unescaped
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:max_len]
