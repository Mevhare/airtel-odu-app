"""Decoding for the ODU's stored SMS.

The modem hands messages back exactly as it stored them: bodies (and sometimes
sender names) as UCS-2 hex, timestamps as a comma-separated local-time tuple.
Nothing here talks to the device -- see ``odu.sms_list``.

The reason this file matters more than "read my texts" suggests: Airtel sends a
usage summary every morning giving *yesterday's* total for the line. That is the
operator's own billing figure, it arrives whether or not this dashboard was
running, and the modem keeps around 40 of them. It is the only source of truth
for days we did not watch.
"""

import calendar
import re

# "Dear Customer, your data usage on 9116135601 for 2026-08-12 was 37206.16 MB."
USAGE_SMS = re.compile(
    r"data usage on (?P<msisdn>\d[\d ]{6,})\s+for\s+(?P<date>\d{4}-\d{2}-\d{2})"
    r"\s+was\s+(?P<amount>[\d.,]+)\s*(?P<unit>[KMGT]B)",
    re.IGNORECASE,
)

UNITS = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}

# tag values the firmware uses on stored messages
READ, UNREAD = "0", "1"


def decode(text):
    """UCS-2 hex to text, passing plain strings through untouched.

    Alphanumeric senders such as "Airtel" arrive already decoded, so anything
    that is not an even-length run of hex digits is returned as-is.
    """
    if not text:
        return ""
    if len(text) % 4 or re.search(r"[^0-9A-Fa-f]", text):
        return text
    try:
        return bytes.fromhex(text).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return text


def parse_date(raw):
    """'26,08,13,05,56,03,+4' -> unix timestamp.

    Fields are YY,MM,DD,hh,mm,ss then the sender's UTC offset in quarter hours.
    The clock part is local to that offset, so it is converted rather than
    trusted as-is -- the ODU and this PC are not guaranteed to agree.
    """
    parts = (raw or "").split(",")
    if len(parts) < 6:
        return None
    try:
        year, month, mday, hour, minute, second = (int(p) for p in parts[:6])
    except ValueError:
        return None

    offset = 0
    if len(parts) > 6:
        try:
            offset = int(parts[6]) * 15 * 60
        except ValueError:
            offset = 0

    stamp = (year + 2000, month, mday, hour, minute, second, 0, 0, 0)
    try:
        return int(calendar.timegm(stamp)) - offset
    except (OverflowError, ValueError):
        return None


def normalise(message):
    """One raw firmware record -> the shape the dashboard uses."""
    body = decode(message.get("content"))
    return {
        "id": message.get("id"),
        "from": decode(message.get("number")),
        "body": body,
        "ts": parse_date(message.get("date")),
        "unread": str(message.get("tag")) == UNREAD,
        "usage": usage_report(body),
    }


def usage_report(body):
    """The daily figure Airtel quotes, or None for an ordinary message."""
    match = USAGE_SMS.search(body or "")
    if not match:
        return None
    try:
        amount = float(match.group("amount").replace(",", ""))
    except ValueError:
        return None
    return {
        "msisdn": match.group("msisdn").replace(" ", ""),
        "date": match.group("date"),
        "bytes": int(amount * UNITS[match.group("unit").upper()]),
    }


def format_number(msisdn):
    """Nigerian mobile numbers are quoted without their leading zero."""
    digits = re.sub(r"\D", "", msisdn or "")
    if len(digits) == 10:
        digits = "0" + digits
    elif digits.startswith("234"):
        digits = "0" + digits[3:]
    if len(digits) == 11:
        return "%s %s %s" % (digits[:4], digits[4:7], digits[7:])
    return digits or None
