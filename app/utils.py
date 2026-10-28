from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings

TZ = ZoneInfo(settings.timezone)
PAGE_SIZE = 25


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def day_bounds(day: date | None = None) -> tuple[datetime, datetime]:
    """UTC start/end of a hospital-local calendar day."""
    day = day or today_local()
    start = datetime.combine(day, time.min, tzinfo=TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def local_to_utc(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=TZ).astimezone(timezone.utc)


def blank(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def parse_int(v: str | None, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    v = blank(v)
    if v is None:
        return None
    try:
        n = int(v)
    except ValueError:
        raise ValueError(f"{v!r} is not a whole number")
    if (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
        raise ValueError(f"{n} is out of range")
    return n


def parse_decimal(v: str | None, *, minimum: Decimal | None = Decimal(0)) -> Decimal | None:
    v = blank(v)
    if v is None:
        return None
    try:
        d = Decimal(v.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{v!r} is not a number")
    if not d.is_finite() or (minimum is not None and d < minimum):
        raise ValueError(f"{v} is out of range")
    return d


def parse_date(v: str | None) -> date | None:
    v = blank(v)
    if v is None:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{v!r} is not a valid date")


def get_or_404(db: Session, model, id_: int):
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404)
    return obj


class Page:
    def __init__(self, items, page: int, total: int):
        self.items, self.page, self.total = items, page, total
        self.pages = max(1, -(-total // PAGE_SIZE))
        self.has_prev, self.has_next = page > 1, page < self.pages


def paginate(db: Session, stmt, page: int) -> Page:
    page = max(1, page)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).unique().all()
    return Page(items, page, total or 0)
