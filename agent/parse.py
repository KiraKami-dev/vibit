"""
From the verbatim strings in evidence records to values: numbers, dates, units, sender addresses.

Pure and model-free. A value whose reading depends on a format the page did not state is rejected with a ParseError
instead of guessed ("1.234" may be one thousand or one and a bit; "03/04/2026" may be March or April). Where the
string itself is unambiguous ("1.234,56", "13/04/2026") its own shape decides, even against a stated format.
"""
import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from calc import Unit, parse_unit

__all__ = ["ParseError", "Unit", "parse_date", "parse_datetime", "parse_decimal", "parse_int", "parse_timezone",
           "parse_unit", "split_address"]


class ParseError(ValueError):
    """A shown value cannot be read without guessing."""


# ---------------------------------------------------------------- numbers

# languages that write the decimal comma, and those that write the decimal point
_COMMA_LANGUAGES = {"de", "fr", "it", "es", "nl", "pt", "pl", "da", "sv", "nb", "no", "fi", "cs", "tr", "el"}
_POINT_LANGUAGES = {"en", "ja", "zh", "ko"}
_LOCALE_TAG = re.compile(r"\(?\b([a-z]{2})[-_]([A-Z]{2})\b\)?")
_SPACES = "    "
# digits with "." "," "'" separators, or a space followed by exactly three digits (a grouping space);
# a digit glued to a letter ("m3", "M12") is part of a unit or a name, not a number
_NUMBER = re.compile(rf"(?<![A-Za-z\d.,])[-+−]?\d+(?:[.,'’]\d+|[{_SPACES}]\d{{3}}(?!\d))*")


def _grouped(head: str, groups: list[str]) -> bool:
    return 1 <= len(head) <= 3 and all(len(g) == 3 for g in groups) and (head != "0" or not groups)


def parse_decimal(text: str, number_format: str = "") -> Decimal:
    """
    The one number in a shown value, units, currency signs and locale tags dropped.

    `number_format` is "de" (1.234,56), "en" (1,234.56) or "" (not stated). A locale tag inside the string, such as
    "(de-DE)", decides the format itself. The format is only consulted when the string could be read both ways.
    """
    tag = _LOCALE_TAG.search(text)
    if tag:
        language = tag.group(1)
        if language in _COMMA_LANGUAGES | _POINT_LANGUAGES:
            number_format = "de" if language in _COMMA_LANGUAGES else "en"
        text = text[:tag.start()] + " " + text[tag.end():]
    tokens = _NUMBER.findall(text)
    if not tokens:
        raise ParseError(f"no number in {text.strip()!r}")
    if len(tokens) > 1:
        raise ParseError(f"several numbers in {text.strip()!r}")
    token = re.sub(rf"[{_SPACES}'’]", "", tokens[0]).replace("−", "-")
    sign, body = ("-", token[1:]) if token[0] == "-" else ("", token.lstrip("+"))

    dots, commas = body.count("."), body.count(",")
    if dots and commas:  # the separator written last is the decimal one
        decimal_sep = "." if body.rfind(".") > body.rfind(",") else ","
        group_sep = "," if decimal_sep == "." else "."
        whole, _, fraction = body.rpartition(decimal_sep)
        head, *groups = whole.split(group_sep)
        if group_sep in fraction or not _grouped(head, groups):
            raise ParseError(f"malformed number {tokens[0]!r}")
        digits = whole.replace(group_sep, "") + "." + fraction
    elif dots or commas:
        sep = "." if dots else ","
        head, *rest = body.split(sep)
        if len(rest) > 1:  # repeated separator: thousands groups
            if not _grouped(head, rest):
                raise ParseError(f"malformed number {tokens[0]!r}")
            digits = head + "".join(rest)
        elif len(rest[0]) == 3 and _grouped(head, rest):  # "1.234" or "1,234": both readings are possible
            if number_format not in ("de", "en"):
                raise ParseError(f"ambiguous number {tokens[0]!r}: number format not stated")
            is_group = (sep == ".") == (number_format == "de")
            digits = head + rest[0] if is_group else head + "." + rest[0]
        else:  # cannot be a thousands group, so it is the decimal separator
            digits = head + "." + rest[0]
    else:
        digits = body
    try:
        return Decimal(sign + digits)
    except InvalidOperation as e:  # pragma: no cover - the token pattern only lets digits through
        raise ParseError(f"malformed number {tokens[0]!r}") from e


def parse_int(text: str, number_format: str = "") -> int:
    """A whole number; a value with a fractional part is rejected."""
    value = parse_decimal(text, number_format)
    if value != value.to_integral_value():
        raise ParseError(f"{text.strip()!r} is not a whole number")
    return int(value)


# ---------------------------------------------------------------- timezones

_ZONE_ABBREVIATIONS = {"z": 0, "utc": 0, "gmt": 0, "wet": 0, "west": 1, "cet": 1, "mez": 1, "cest": 2, "mesz": 2,
                       "eet": 2, "eest": 3}
_OFFSET = re.compile(r"(?:\b(?:UTC|GMT)\s*)?([+−-])\s*(\d{1,2})(?::?(\d{2}))?(?!\d)", re.IGNORECASE)
_IANA = re.compile(r"\b[A-Za-z_]+(?:/[A-Za-z_+\-]+)+\b")
_ABBREVIATION = re.compile(r"\b(" + "|".join(_ZONE_ABBREVIATIONS) + r")\b", re.IGNORECASE)


def _find_timezone(text: str) -> tzinfo | None:
    """A timezone named somewhere in the text: IANA name, UTC offset or a common abbreviation; None if there is none."""
    iana = _IANA.search(text)
    if iana:
        try:
            return ZoneInfo(iana.group(0))
        except (KeyError, ValueError, OSError):  # unknown name or no zone database on this machine
            pass
    offset = _OFFSET.search(text)
    if offset:
        hours, minutes = int(offset.group(2)), int(offset.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ParseError(f"impossible UTC offset in {text.strip()!r}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if offset.group(1) == "+" else -delta)
    abbreviation = _ABBREVIATION.search(text)
    if abbreviation:
        return timezone(timedelta(hours=_ZONE_ABBREVIATIONS[abbreviation.group(1).lower()]))
    return None


def parse_timezone(text: str) -> tzinfo | None:
    """The stated timezone ("+02:00", "UTC", "UTC+2", "Europe/Athens", "MESZ"); None for an empty statement."""
    if not text.strip():
        return None
    zone = _find_timezone(text)
    if zone is None:
        raise ParseError(f"unknown timezone {text.strip()!r}")
    return zone


# ---------------------------------------------------------------- dates

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,6})\d*)?)?)?"
                  r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,4})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{1,4})(?!\d)")
_DAY_MONTH_NAME = re.compile(r"(?<!\d)(\d{1,2})\.?\s+([^\W\d_]{3,})\.?,?\s+(\d{4})(?!\d)")
_MONTH_NAME_DAY = re.compile(r"\b([^\W\d_]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)")
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?\s*([ap])\.?m\.?(?![a-z])|"
                   r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?", re.IGNORECASE)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "mae": 3, "mrz": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12}


def _build_date(year: int, month: int, day: int, text: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as e:
        raise ParseError(f"impossible date {text.strip()!r}") from e


def _year(text: str) -> int:
    return int(text) + 2000 if len(text) <= 2 else int(text)


def _split_date(text: str, date_order: str) -> tuple[date, str]:
    """The calendar date in the text and the text left over (time, zone)."""
    m = _NUMERIC_DATE.search(text)
    if m:
        rest = text[:m.start()] + " " + text[m.end():]
        a, b, c = m.group(1), m.group(3), m.group(4)
        if len(a) == 4:  # year first
            return _build_date(int(a), int(b), int(c), text), rest
        if len(a) > 2 or len(c) not in (2, 4):
            raise ParseError(f"malformed date {text.strip()!r}")
        first, second, year = int(a), int(b), _year(c)
        if first > 12 and second > 12:
            raise ParseError(f"impossible date {text.strip()!r}")
        if first > 12 or (first <= 12 >= second and date_order == "dmy") or first == second:
            day, month = first, second
        elif second > 12 or date_order == "mdy":
            day, month = second, first
        else:
            raise ParseError(f"ambiguous date {text.strip()!r}: order of day and month not stated")
        return _build_date(year, month, day, text), rest
    for pattern, day_group, month_group in ((_DAY_MONTH_NAME, 1, 2), (_MONTH_NAME_DAY, 2, 1)):
        m = pattern.search(text)
        month = _MONTHS.get(m.group(month_group)[:3].lower()) if m else None
        if m and month:
            rest = text[:m.start()] + " " + text[m.end():]
            return _build_date(int(m.group(3)), month, int(m.group(day_group)), text), rest
    raise ParseError(f"no date in {text.strip()!r}")


def parse_date(text: str, date_order: str = "") -> date:
    """The calendar date as written (no timezone conversion), e.g. a validity bound."""
    iso = _ISO.fullmatch(text.strip())
    if iso:
        return _build_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), text)
    return _split_date(text, date_order)[0]


def parse_datetime(text: str, date_order: str = "", timezone_text: str = "") -> datetime:
    """
    A shown date or timestamp as a timezone-aware UTC datetime.

    ISO 8601 strings parse directly. Otherwise `date_order` ("dmy", "mdy", "ymd", "") decides between day-first and
    month-first when the string allows both. The zone is the one written in the string, else `timezone_text` (the
    zone the page states), else UTC. A date without a time is the start of that day in that zone; so a date-only
    value with no timezone anywhere is a UTC date.
    """
    text = text.strip()
    if not text:
        raise ParseError("no date")
    stated = parse_timezone(timezone_text)
    iso = _ISO.fullmatch(text)
    if iso:
        year, month, day, hour, minute, second, fraction, offset = iso.groups()
        day_ = _build_date(int(year), int(month), int(day), text)
        zone = _find_timezone(offset) if offset else stated
        micro = int((fraction or "0").ljust(6, "0"))
        clock = (int(hour or 0), int(minute or 0), int(second or 0), micro)
    else:
        day_, rest = _split_date(text, date_order)
        clock = (0, 0, 0, 0)
        t = _TIME.search(rest)
        if t:
            rest = rest[:t.start()] + " " + rest[t.end():]
            if t.group(4):  # 12-hour clock
                hour = int(t.group(1)) % 12 + (12 if t.group(4).lower() == "p" else 0)
                clock = (hour, int(t.group(2)), int(t.group(3) or 0), 0)
            else:
                clock = (int(t.group(5)), int(t.group(6)), int(t.group(7) or 0), 0)
        zone = _find_timezone(rest) or stated
    try:
        local = datetime(day_.year, day_.month, day_.day, *clock, tzinfo=zone or UTC)
    except ValueError as e:
        raise ParseError(f"impossible time in {text!r}") from e
    return local.astimezone(UTC)


# ---------------------------------------------------------------- addresses

_ADDRESS = re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*)")


def split_address(text: str) -> tuple[str, str]:
    """(local part, domain) of the one address in a shown sender, lower-cased; a display name is dropped."""
    found = {(local.lower(), domain.lower()) for local, domain in _ADDRESS.findall(text)}
    if len(found) != 1:
        raise ParseError(f"{'no' if not found else 'several'} sender address in {text.strip()!r}")
    return found.pop()
