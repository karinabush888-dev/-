from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def utc_day_key(dt: datetime | None = None) -> str:
    d = dt or utc_now()
    return d.strftime("%Y-%m-%d")


def seconds_until_next_utc_day(now: datetime | None = None) -> int:
    n = now or utc_now()
    nxt = datetime(year=n.year, month=n.month, day=n.day, tzinfo=timezone.utc) + timedelta(days=1)
    return int((nxt - n).total_seconds())
