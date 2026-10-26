"""
The ``orders_data`` dataset and the calendar oracle the grain tests compare against.

Shared by the DuckDB unit tests and the warehouse integration tests, so both check the
same rows against the same expectations.
"""

from __future__ import annotations

import datetime
from typing import Any

ORDERS_ROWS: tuple[tuple[datetime.datetime, int], ...] = (
    (datetime.datetime(2024, 3, 31, 23, 59, 58), 1),
    (datetime.datetime(2024, 3, 31, 23, 59, 59), 2),
    (datetime.datetime(2024, 4, 1, 0, 0, 0), 4),
    (datetime.datetime(2024, 4, 1, 0, 0, 0), 8),
    (datetime.datetime(2024, 4, 1, 0, 30, 15), 16),
    (datetime.datetime(2024, 4, 3, 12, 0, 0), 32),
    (datetime.datetime(2024, 4, 7, 9, 15, 0), 64),
    (datetime.datetime(2024, 4, 8, 9, 15, 0), 128),
    (datetime.datetime(2024, 12, 31, 18, 0, 0), 256),
    (datetime.datetime(2025, 1, 1, 6, 0, 0), 512),
)
"""
``orders_data`` rows: ``(ordered_at, amount)``, with ``ordered_on`` the date of ``ordered_at``.

Each amount is a distinct power of two, so every grouping's sums are distinct and a row
counted in the wrong period changes a total. The timestamps straddle a second, minute and
hour boundary (23:59:58 to 00:00:00), a day, an ISO week (Sunday 31 March and 7 April
against the Mondays after), a month and quarter (31 March to 1 April) and a year.
"""


def orders_values_sql() -> str:
    """
    Render :data:`ORDERS_ROWS` as the ``VALUES`` list of an ``INSERT``, valid on all backends.

    Returns:
        ``(TIMESTAMP '...', DATE '...', amount), ...``.
    """
    return ", ".join(
        f"(TIMESTAMP '{at:%Y-%m-%d %H:%M:%S}', DATE '{at:%Y-%m-%d}', {amount})"
        for at, amount in ORDERS_ROWS
    )


def truncate(value: datetime.datetime, unit: str) -> datetime.date | datetime.datetime:
    """
    Truncate a timestamp to a grain in Python: the oracle the grain tests compare against.

    Written from the calendar, not from any backend's ``DATE_TRUNC``. Weeks start on
    Monday. A day or coarser gives a ``date``, a finer grain a ``datetime``.

    Args:
        value: The timestamp.
        unit: A grain.

    Returns:
        The start of the period ``value`` falls in.
    """
    day = value.date()
    if unit == "year":
        return day.replace(month=1, day=1)
    if unit == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if unit == "month":
        return day.replace(day=1)
    if unit == "week":
        return day - datetime.timedelta(days=day.weekday())
    if unit == "day":
        return day
    if unit == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    if unit == "minute":
        return value.replace(second=0, microsecond=0)
    if unit == "second":
        return value.replace(microsecond=0)
    raise ValueError(unit)


def expected_by_grain(unit: str, *, from_date: bool = False) -> list[tuple[int, Any]]:
    """
    Return ``(revenue, period)`` rows for revenue by one grain, ordered by period.

    Args:
        unit: A grain.
        from_date: Truncate ``ordered_on`` (the date) rather than ``ordered_at``.

    Returns:
        The rows a grain query ordered by its period should return.
    """
    totals: dict[Any, int] = {}
    for at, amount in ORDERS_ROWS:
        source = datetime.datetime.combine(at.date(), datetime.time()) if from_date else at
        period = truncate(source, unit)
        totals[period] = totals.get(period, 0) + amount
    return [(total, period) for period, total in sorted(totals.items())]
