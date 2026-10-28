import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
from .permissions import can

PASSWORD_MIN = 10
PASSWORD_MAX = 72  # bcrypt's input limit
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def password_problem(password: str) -> str | None:
    if len(password) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters."
    if len(password.encode()) > PASSWORD_MAX:
        return f"Password must be at most {PASSWORD_MAX} bytes."
    if password.isalpha() or password.isdigit():
        return "Password must mix letters with numbers or symbols."
    return None


def temporary_password() -> str:
    return "Cp-" + secrets.token_urlsafe(9)


def start_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session.update(uid=user.id, av=user.auth_version, csrf=secrets.token_urlsafe(32))


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active or user.auth_version != request.session.get("av"):
        request.session.pop("uid", None)
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(nxt if request.method == "GET" else "/")
    request.state.user = user
    if user.must_change_password and request.url.path not in ("/account/password", "/logout"):
        raise PasswordChangeRequired()
    return user


def require(*perms: str):
    """Dependency: signed-in user holding at least one of `perms`."""

    def dependency(user: User = Depends(current_user)) -> User:
        if not any(can(user, p) for p in perms):
            raise Forbidden()
        return user

    return dependency


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


async def csrf_protect(request: Request) -> None:
    """App-wide dependency: every state-changing request must echo the session token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token")
    expected = request.session.get("csrf")
    if not sent or not expected or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(403, "Your session expired or the form was stale. Go back, reload the page and try again.")
