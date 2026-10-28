from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, utcnow
from ..security import (
    LOCKOUT_MINUTES, MAX_FAILED_LOGINS, current_user, hash_password, password_problem, start_session, verify_password,
)
from ..services.audit import audit
from ..templating import flash, redirect, render

router = APIRouter()


def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if request.session.get("uid"):
        return redirect("/")
    return render(request, "auth/login.html", next=_safe_next(next))


@router.post("/login")
def login(request: Request, email: Annotated[str, Form()] = "", password: Annotated[str, Form()] = "",
          next: Annotated[str, Form()] = "/", db: Session = Depends(get_db)):
    email = email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email)) if email else None
    now = utcnow()

    def fail(message: str):
        return render(request, "auth/login.html", status_code=401, error=message, email=email, next=_safe_next(next))

    if user and user.locked_until and user.locked_until > now:
        mins = max(1, int((user.locked_until - now).total_seconds() // 60))
        return fail(f"Too many failed attempts. This account is locked for another {mins} min.")

    if not user or not verify_password(password, user.password_hash):
        if user:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_logins = 0
                audit(db, request, user, "auth.locked", "user", user.id)
            db.commit()
        return fail("Incorrect email or password.")

    if not user.is_active:
        return fail("This account has been deactivated. Contact the hospital administrator.")

    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    audit(db, request, user, "auth.login", "user", user.id)
    db.commit()
    start_session(request, user)
    return redirect(_safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/account/password")
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account/password.html")


@router.post("/account/password")
def change_password(request: Request, current: Annotated[str, Form()] = "", new: Annotated[str, Form()] = "",
                    confirm: Annotated[str, Form()] = "", user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    error = None
    if not verify_password(current, user.password_hash):
        error = "Your current password is incorrect."
    elif new != confirm:
        error = "The new passwords don't match."
    elif new == current:
        error = "Choose a password different from the current one."
    else:
        error = password_problem(new)
    if error:
        return render(request, "account/password.html", status_code=422, error=error)

    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.auth_version += 1  # signs out this user's other sessions
    audit(db, request, user, "auth.password_changed", "user", user.id)
    db.commit()
    start_session(request, user)
    flash(request, "Password updated. Other sessions for your account have been signed out.")
    return redirect("/")
