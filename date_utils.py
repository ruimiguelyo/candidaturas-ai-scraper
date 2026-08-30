import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any


RECENCY_WINDOW_DAYS = 7

MONTHS = {
    "jan": 1,
    "january": 1,
    "fev": 2,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "abr": 4,
    "apr": 4,
    "april": 4,
    "mai": 5,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "ago": 8,
    "aug": 8,
    "august": 8,
    "set": 9,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "out": 10,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dez": 12,
    "dec": 12,
    "december": 12,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def parse_publication_date(value: Any, now: datetime | None = None) -> datetime | None:
    """Parse the date formats returned by the supported job boards."""
    reference = _aware(now or utc_now())
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None
    text = _ascii(raw).casefold().strip()
    if text in {"recente", "recent", "n/a", "na", "unknown", "desconhecida"}:
        return None
    if text in {"hoje", "today", "just now", "agora"}:
        return reference
    if text in {"ontem", "yesterday"}:
        return reference - timedelta(days=1)

    relative = re.search(
        r"(?:ha\s+)?(\d+)\s*"
        r"(minutes?|mins?|minutos?|hours?|hrs?|horas?|days?|dias?|weeks?|semanas?|months?|mes(?:es)?)"
        r"(?:\s+ago)?",
        text,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith(("minute", "min", "minuto")):
            return reference - timedelta(minutes=amount)
        if unit.startswith(("hour", "hr", "hora")):
            return reference - timedelta(hours=amount)
        if unit.startswith(("day", "dia")):
            return reference - timedelta(days=amount)
        if unit.startswith(("week", "semana")):
            return reference - timedelta(days=amount * 7)
        return reference - timedelta(days=amount * 30)

    numeric = text
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", numeric):
        return parse_publication_date(float(numeric), reference)

    iso_candidate = raw.replace("Z", "+00:00")
    try:
        return _aware(datetime.fromisoformat(iso_candidate))
    except ValueError:
        pass

    try:
        parsed_email_date = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed_email_date = None
    if parsed_email_date is not None:
        return _aware(parsed_email_date)

    for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    short_date = re.fullmatch(r"(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?", text)
    if short_date:
        day = int(short_date.group(1))
        month = MONTHS.get(short_date.group(2))
        if month:
            year = int(short_date.group(3) or reference.year)
            try:
                candidate = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
            if not short_date.group(3) and candidate.date() > reference.date() + timedelta(days=1):
                candidate = candidate.replace(year=year - 1)
            return candidate
    return None


def recency_status(
    value: Any,
    days: int = RECENCY_WINDOW_DAYS,
    now: datetime | None = None,
) -> tuple[bool, datetime | None, str]:
    reference = _aware(now or utc_now())
    published = parse_publication_date(value, reference)
    if published is None:
        return False, None, "missing_post_date"
    if published > reference + timedelta(days=1):
        return False, published, "invalid_post_date"
    oldest_allowed = reference.date() - timedelta(days=days)
    if published.date() < oldest_allowed:
        return False, published, "outside_recency_window"
    return True, published, "recent"
