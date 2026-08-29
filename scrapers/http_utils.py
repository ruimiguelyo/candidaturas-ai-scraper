import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Generic, Iterable, TypeVar


ResponseT = TypeVar("ResponseT")
ItemT = TypeVar("ItemT")
TRANSIENT_STATUS_CODES = {429}
MAX_RETRY_DELAY_SECONDS = 15.0


class ScrapeResults(list[ItemT], Generic[ItemT]):
    """List of collected jobs with observable partial-result metadata."""

    def __init__(
        self,
        items: Iterable[ItemT] = (),
        *,
        partial: bool = False,
        warning: str | None = None,
    ) -> None:
        super().__init__(items)
        self.partial = partial
        self.warning = warning

    def mark_partial(self, warning: str) -> None:
        self.partial = True
        self.warning = warning


def is_transient_status(status_code: int) -> bool:
    return status_code in TRANSIENT_STATUS_CODES or 500 <= status_code <= 599


def is_timeout_error(error: Exception) -> bool:
    """Recognise timeout errors from httpx, curl-cffi and asyncio."""
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True
    if getattr(error, "code", None) == 28:
        return True
    error_name = type(error).__name__.casefold()
    error_text = str(error).casefold()
    return any(
        marker in f"{error_name} {error_text}"
        for marker in ("timeout", "timed out", "curl: (28)", "curl code: 28")
    )


def _retry_delay(response: object | None, fallback: float) -> float:
    """Honour a numeric Retry-After header without allowing excessive waits."""
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        requested_delay = float(retry_after)
    except (TypeError, ValueError):
        requested_delay = fallback
    return max(0.0, min(requested_delay, MAX_RETRY_DELAY_SECONDS))


async def get_with_retry(
    get: Callable[..., Awaitable[ResponseT]],
    url: str,
    *,
    retries: int = 2,
    base_delay: float = 0.5,
    **kwargs: Any,
) -> ResponseT:
    """Retry only rate limits, server errors and timeouts, with a short backoff."""
    response: ResponseT | None = None
    for attempt in range(retries + 1):
        response = None
        try:
            response = await get(url, **kwargs)
        except Exception as error:
            if attempt >= retries or not is_timeout_error(error):
                raise
        else:
            status_code = int(getattr(response, "status_code", 0))
            if not is_transient_status(status_code) or attempt >= retries:
                return response

        await asyncio.sleep(_retry_delay(response, base_delay * (2**attempt)))

    raise RuntimeError("Retry loop ended unexpectedly")
