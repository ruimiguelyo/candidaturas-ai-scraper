"""Parsing preciso de datas e cálculo de frescura em UTC."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

MONTHS = {
    "jan": 1, "janeiro": 1, "january": 1,
    "fev": 2, "fevereiro": 2, "feb": 2, "february": 2,
    "mar": 3, "marco": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "mai": 5, "maio": 5, "may": 5,
    "jun": 6, "junho": 6, "june": 6,
    "jul": 7, "julho": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "set": 9, "setembro": 9, "sep": 9, "sept": 9, "september": 9,
    "out": 10, "outubro": 10, "oct": 10, "october": 10,
    "nov": 11, "novembro": 11, "november": 11,
    "dez": 12, "dezembro": 12, "dec": 12, "december": 12,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ascii_lower(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def parse_publication_datetime(value: Any, now: datetime | None = None) -> datetime | None:
    """Converte qualquer formato comum de data (ISO, RFC2822, epoch, relativo PT/EN) em UTC datetime."""
    reference = ensure_utc(now or utc_now())
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 12, 0, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None

    text = _ascii_lower(raw)
    if text in {"recente", "recent", "n/a", "na", "unknown", "desconhecida", "none", "null"}:
        return None
    if text in {"hoje", "today", "just now", "agora", "agora mesmo"}:
        return reference
    if text in {"ontem", "yesterday"}:
        return reference - timedelta(days=1)

    # Numeros Unix em string
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", text):
        return parse_publication_datetime(float(text), reference)

    # Expressões relativas PT/EN: "há 20 minutos", "no site há 2 semanas", "3 hours ago", "2d ago"
    rel_long = re.search(
        r"(?:ha\s+(?:cerca\s+de\s+|mais\s+de\s+)?)?(\d+)\s*"
        r"(seconds?|secs?|segundos?|minutes?|mins?|minutos?|hours?|hrs?|horas?|days?|dias?|weeks?|semanas?|months?|mes(?:es)?)"
        r"(?:\s+ago)?",
        text,
    )
    if rel_long:
        amount = int(rel_long.group(1))
        unit = rel_long.group(2)
        if unit.startswith(("sec", "seg")):
            return reference - timedelta(seconds=amount)
        if unit.startswith(("min",)):
            return reference - timedelta(minutes=amount)
        if unit.startswith(("hour", "hr", "hora")):
            return reference - timedelta(hours=amount)
        if unit.startswith(("day", "dia")):
            return reference - timedelta(days=amount)
        if unit.startswith(("week", "semana")):
            return reference - timedelta(days=amount * 7)
        return reference - timedelta(days=amount * 30)

    rel_compact = re.fullmatch(r"(\d+)\s*([mhdw])(?:\s+ago)?", text)
    if rel_compact:
        amount = int(rel_compact.group(1))
        unit = rel_compact.group(2)
        if unit == "m":
            return reference - timedelta(minutes=amount)
        if unit == "h":
            return reference - timedelta(hours=amount)
        if unit == "d":
            return reference - timedelta(days=amount)
        if unit == "w":
            return reference - timedelta(days=amount * 7)

    # ISO-8601 (preserva hora exata)
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        parsed_iso = datetime.fromisoformat(iso_candidate)
        if parsed_iso.hour == 0 and parsed_iso.minute == 0 and parsed_iso.second == 0 and len(raw) <= 10:
            parsed_iso = parsed_iso.replace(hour=12)
        return ensure_utc(parsed_iso)
    except ValueError:
        pass

    # RFC-2822 (RSS feeds Teamtailor, etc.)
    try:
        parsed_email = parsedate_to_datetime(raw)
        if parsed_email is not None:
            return ensure_utc(parsed_email)
    except (TypeError, ValueError, OverflowError):
        pass

    # Formatos numéricos PT/ISO
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(hour=12, tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Datas curtas: "18 nov", "19nov", "18 de nov de 2026", "24 Nov 2026"
    short_match = re.search(r"\b(\d{1,2})\s*(?:de\s+)?([a-z]+)(?:\s*(?:de\s+)?(\d{4}))?\b", text)
    if short_match:
        day = int(short_match.group(1))
        month = MONTHS.get(short_match.group(2))
        if month:
            year = int(short_match.group(3) or reference.year)
            try:
                candidate = datetime(year, month, day, 12, 0, tzinfo=timezone.utc)
            except ValueError:
                return None
            if not short_match.group(3) and candidate.date() > reference.date() + timedelta(days=2):
                candidate = candidate.replace(year=year - 1)
            return candidate

    # Datas em formato inglês Mês Dia[, Ano]: "Nov 17, 2026", "Oct 26", "November 24, 2026"
    mdy_match = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:,?\s*(\d{4}))?\b", text)
    if mdy_match:
        month = MONTHS.get(mdy_match.group(1))
        day = int(mdy_match.group(2))
        if month:
            year = int(mdy_match.group(3) or reference.year)
            try:
                candidate = datetime(year, month, day, 12, 0, tzinfo=timezone.utc)
            except ValueError:
                return None
            if not mdy_match.group(3) and candidate.date() > reference.date() + timedelta(days=2):
                candidate = candidate.replace(year=year - 1)
            return candidate

    return None


def recency_check(
    published_at: datetime | None,
    max_days: int = 14,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Verifica se a data de publicação está dentro da janela configurada."""
    reference = ensure_utc(now or utc_now())
    if published_at is None:
        return False, "missing_post_date"

    dt = ensure_utc(published_at)
    if dt > reference + timedelta(hours=36):
        return False, "invalid_future_date"

    oldest_allowed = reference - timedelta(days=max_days, hours=12)
    if dt < oldest_allowed:
        return False, "outside_recency_window"

    return True, "recent"


def format_freshness(published_at: datetime | None, now: datetime | None = None) -> str:
    """Devolve etiqueta legível de frescura em PT-PT."""
    if published_at is None:
        return "Data desc."
    reference = ensure_utc(now or utc_now())
    dt = ensure_utc(published_at)
    delta = reference - dt
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes < 60:
        return f"Há {max(1, total_minutes)}m"
    total_hours = total_minutes // 60
    if total_hours < 24:
        return f"Há {total_hours}h"
    days = delta.days
    if days == 1:
        return "Ontem"
    if days < 7:
        return f"Há {days}d"
    weeks = max(1, days // 7)
    return f"Há {weeks}sem"
