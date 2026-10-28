from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode, urlparse

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import models
from .config import BASE_DIR, settings
from .permissions import ROLE_SUMMARIES, can
from .security import csrf_token
from .utils import TZ, today_local

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# (section, [(label, href, lucide icon, permission)])
NAV = [
    ("Overview", [
        ("Dashboard", "/", "layout-dashboard", "dashboard"),
    ]),
    ("Front desk", [
        ("Patients", "/patients", "users", "patients.view"),
        ("Appointments", "/appointments", "calendar-days", "appointments.view"),
        ("Visit queue", "/visits", "stethoscope", "visits.view"),
    ]),
    ("Inpatient", [
        ("Wards & beds", "/wards", "bed-double", "wards.view"),
        ("Admissions", "/admissions", "clipboard-list", "wards.view"),
    ]),
    ("Services", [
        ("Pharmacy", "/pharmacy", "pill", "pharmacy.view"),
        ("Drug inventory", "/pharmacy/inventory", "package", "pharmacy.inventory"),
        ("Lab worklist", "/lab", "flask-conical", "lab.view"),
        ("Lab tests", "/lab/tests", "test-tubes", "lab.catalog"),
    ]),
    ("Finance", [
        ("Billing", "/billing", "receipt", "billing.view"),
        ("Collections", "/billing/payments", "wallet", "billing.view"),
    ]),
    ("Administration", [
        ("Staff", "/admin/users", "user-cog", "admin.users"),
        ("Departments", "/admin/departments", "building-2", "admin.config"),
        ("Ward setup", "/admin/wards", "hospital", "admin.config"),
        ("Audit log", "/admin/audit", "scroll-text", "admin.audit"),
    ]),
]


def nav_for(user) -> list:
    out = []
    for section, items in NAV:
        visible = [i for i in items if can(user, i[3])]
        if visible:
            out.append((section, visible))
    return out


def nav_active(request: Request, href: str) -> bool:
    path = request.url.path
    if href == "/":
        return path == "/"
    # Longest matching nav entry wins so /pharmacy/inventory doesn't also light /pharmacy.
    matches = [i[1] for _, items in NAV for i in items if i[1] != "/" and (path == i[1] or path.startswith(i[1] + "/"))]
    return bool(matches) and max(matches, key=len) == href


# ---------------------------------------------------------------- filters

def fmt_dt(value: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not value:
        return "—"
    return value.astimezone(TZ).strftime(fmt)


def fmt_time(value: datetime | None) -> str:
    return fmt_dt(value, "%H:%M")


def fmt_date(value: date | datetime | None, fmt: str = "%d %b %Y") -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        value = value.astimezone(TZ)
    return value.strftime(fmt)


def money(value: Decimal | int | float | None, symbol: bool = True) -> str:
    v = Decimal(value or 0)
    s = f"{v:,.2f}"
    return f"{settings.currency} {s}" if symbol else s


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    mins = int((datetime.now(value.tzinfo) - value).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 24:
        return f"{mins // 60} h {mins % 60:02d} m ago"
    return f"{mins // (60 * 24)} d ago"


# Status → badge tone. Anything unlisted renders neutral.
TONES = {
    "scheduled": "info", "checked_in": "accent", "completed": "success", "cancelled": "muted", "no_show": "warn",
    "waiting": "warn", "triaged": "info", "in_progress": "accent",
    "available": "success", "occupied": "danger", "cleaning": "warn", "maintenance": "muted",
    "admitted": "accent", "discharged": "muted",
    "pending": "warn", "dispensed": "success",
    "ordered": "warn", "collected": "info",
    "open": "warn", "partially_paid": "info", "paid": "success", "void": "muted",
    "routine": "muted", "urgent": "warn", "emergency": "danger", "stat": "danger",
}


def tone(value) -> str:
    return TONES.get(str(value), "muted")


def options(items, label: str = "name", value: str = "id") -> list[tuple]:
    """(value, label) pairs for the select() macro."""
    return [(getattr(i, value), getattr(i, label)) for i in items]


def query(request: Request, **changes) -> str:
    """Current query string with some keys replaced — for filters and pagination links."""
    params = dict(request.query_params)
    for k, v in changes.items():
        if v is None or v == "":
            params.pop(k, None)
        else:
            params[k] = v
    return "?" + urlencode(params) if params else request.url.path


templates.env.filters.update(dt=fmt_dt, time=fmt_time, date=fmt_date, money=money, ago=ago, tone=tone)
templates.env.globals.update(
    settings=settings, can=can, models=models, Role=models.Role,
    role_summaries=ROLE_SUMMARIES, nav_active=nav_active, query=query, today=today_local, options=options, now_utc=models.utcnow,
)


# ---------------------------------------------------------------- responses

def flash(request: Request, message: str, category: str = "success") -> None:
    request.session["_flash"] = request.session.get("_flash", []) + [[category, message]]


def render(request: Request, template: str, status_code: int = 200, **ctx):
    user = getattr(request.state, "user", None)
    ctx.update(
        user=user,
        nav=nav_for(user) if user else [],
        csrf_token=csrf_token(request),
        flashes=request.session.pop("_flash", []),
    )
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def back(request: Request, default: str = "/") -> RedirectResponse:
    """Redirect to the same-origin page the form was posted from."""
    ref = urlparse(request.headers.get("referer") or "")
    if ref.netloc == request.url.netloc and ref.path.startswith("/"):
        return redirect(ref.path + (f"?{ref.query}" if ref.query else ""))
    return redirect(default)
