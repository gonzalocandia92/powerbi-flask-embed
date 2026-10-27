"""Audited DeepSeek direct-API peak calendar.

DeepSeek defines peak hours in UTC but excludes Chinese public holidays.  The
holiday *date* is interpreted in China Standard Time.  An unknown year is an
accounting configuration error, never silently treated as a workday.

2026 source: https://en.bjhd.gov.cn/workinginhaidian/supportingservices/publicholidays/202512/t20251211_4797062.shtml
Schedule: https://api-docs.deepseek.com/quick_start/pricing/
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _days(month: int, first: int, last: int) -> set[str]:
    return {f"2026-{month:02d}-{day:02d}" for day in range(first, last + 1)}


CHINA_PUBLIC_HOLIDAYS: dict[int, frozenset[str]] = {
    2026: frozenset().union(
        _days(1, 1, 3), _days(2, 15, 23), _days(4, 4, 6),
        _days(5, 1, 5), _days(6, 19, 21), _days(9, 25, 27),
        _days(10, 1, 7),
    ),
}


def deepseek_pricing_band(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    utc = at.astimezone(timezone.utc)
    china = utc + timedelta(hours=8)
    holidays = CHINA_PUBLIC_HOLIDAYS.get(china.year)
    if holidays is None:
        raise ValueError(f"Chinese holiday calendar missing for {china.year}")
    if china.date().isoformat() in holidays or utc.weekday() >= 5:
        return "off-peak"
    return "peak" if 1 <= utc.hour < 4 or 6 <= utc.hour < 10 else "off-peak"
