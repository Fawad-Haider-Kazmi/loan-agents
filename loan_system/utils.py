from __future__ import annotations

from datetime import date, datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return date.today()


def normalize_id(value: str) -> str:
    """Keep digits only, so '42101-1234567-1' == '4210112345671'."""
    return "".join(ch for ch in value if ch.isdigit())


def money(value: float) -> str:
    return f"PKR {value:,.0f}"
