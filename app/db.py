from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    settings.database_url,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    from . import models  # noqa: F401  テーブル定義の登録

    Base.metadata.create_all(engine)
