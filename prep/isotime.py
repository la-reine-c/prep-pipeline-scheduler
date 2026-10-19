"""Reading Teams' timestamps.

Teams trims trailing zeros from fractional seconds, so the same field comes
as "...10.190Z" one day and "...10.19Z" the next. Python 3.10's
fromisoformat only accepts 3 or 6 digits there, and a timestamp it rejects
reads as "no date" - which, for when prep was set, quietly cut its estimate
to a single day's worth.
"""
from __future__ import annotations

import re
from datetime import datetime

_FRACTION = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")


def fromiso(value: object) -> datetime:
    """datetime.fromisoformat, also taking "Z" and any number of decimals."""
    text = str(value).replace("Z", "+00:00").replace("z", "+00:00")
    text = _FRACTION.sub(lambda m: f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}",
                         text, count=1)
    return datetime.fromisoformat(text)
