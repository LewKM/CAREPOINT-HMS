from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .routers import admin, appointments, auth, billing, dashboard, lab, patients, pharmacy, visits, wards
from .security import Forbidden, LoginRequired, PasswordChangeRequired, csrf_protect
from .templating import redirect, render

app = FastAPI(
    title=settings.app_name,
    dependencies=[Depends(csrf_protect)],
    docs_url=None, redoc_url=None, openapi_url=None,  # HTML app; no public API surface
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="carepoint_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.https_only,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/static"):
        # Pages carry patient data — never let a shared browser or proxy cache them.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    target = "/login" if exc.next_url in ("/", "") else f"/login?next={quote(exc.next_url)}"
    return redirect(target)


@app.exception_handler(PasswordChangeRequired)
async def password_change_required(request: Request, exc: PasswordChangeRequired):
    return redirect("/account/password")


@app.exception_handler(Forbidden)
async def forbidden(request: Request, exc: Forbidden):
    return render(request, "errors/error.html", status_code=403, code=403, title="Not available for your role",
                  message="Your role doesn't include access to this area. If you need it for your work, ask the hospital administrator.")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    titles = {404: "Page not found", 403: "Request refused", 405: "Not allowed"}
    message = exc.detail if exc.status_code != 404 else "That record or page doesn't exist, or it has been removed."
    return render(request, "errors/error.html", status_code=exc.status_code, code=exc.status_code,
                  title=titles.get(exc.status_code, "Something went wrong"), message=message)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


for module in (auth, dashboard, patients, appointments, visits, wards, pharmacy, lab, billing, admin):
    app.include_router(module.router)
