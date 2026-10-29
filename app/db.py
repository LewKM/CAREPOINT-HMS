from fastapi import Request
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


def sqlalchemy_url(url: str) -> str:
    """Accept a plain libpq URL (as Neon hands out) and pin the psycopg 3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


engine = create_engine(
    sqlalchemy_url(settings.database_url),
    # No pool_pre_ping: against a remote Neon it costs a full round trip on every request.
    # TCP keepalives stop NAT dropping idle connections instead; if one dies anyway,
    # SQLAlchemy invalidates the pool on the disconnect error and the next request reconnects.
    pool_recycle=1800,
    pool_size=5,
    max_overflow=10,
    connect_args={
        # Neon's pooler is PgBouncer in transaction mode: no server-side prepared statements.
        "prepare_threshold": None,
        "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
    },
)
read_engine = engine.execution_options(isolation_level="AUTOCOMMIT")  # same pool, see get_db
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def get_db(request: Request):
    # Page loads (GET) only read, plus at most one audit insert, so they run in autocommit:
    # that skips the BEGIN and ROLLBACK round trips. Postgres gives each statement a fresh
    # snapshot either way. Anything that changes data is a POST and keeps a real transaction.
    db = SessionLocal(bind=read_engine) if request.method in ("GET", "HEAD") else SessionLocal()
    try:
        yield db
    finally:
        db.close()
