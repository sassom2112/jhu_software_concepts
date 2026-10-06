"""
models.py - SQLAlchemy 2.x ORM mapping of the existing `applicants` table.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

The Applicant class maps onto the very table load_data.py creates and fills;
SQLAlchemy never creates a second copy of the data (no create_all() here).

Engine and Session:
  * `engine` is created once from the same environment settings psycopg uses
    (get_sqlalchemy_url() below selects the psycopg 3 driver).  Creating
    an engine does not open a connection; connections are pooled, checked
    with pool_pre_ping so a restarted database does not break a long-running process,
    and give up after a 10-second connect timeout.
  * `SessionLocal` is a sessionmaker; use it as a context manager::

        from worker.etl.models import Applicant, SessionLocal
        with SessionLocal() as session:
            first = session.get(Applicant, 1020481)
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, Integer, Text, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import psycopg
from psycopg.conninfo import conninfo_to_dict

from db.db_config import (CONNECT_TIMEOUT_SECONDS, INVALID_SETTINGS_MESSAGE, TABLE_NAME,
                          get_database_url)


def get_sqlalchemy_url() -> URL:
    """The db_config connection as a sqlalchemy.engine.URL that selects the psycopg (v3) driver.

    The settings are parsed by psycopg's own libpq-compatible parser and rebuilt
    field by field, so URLs, key=value strings, Unix-socket directories and IPv6
    hosts all work, extra options such as sslmode are kept, and a DB_PASSWORD
    travels along (SQLAlchemy masks it as ``***`` whenever the URL is printed).  A non-numeric
    port is passed through unchanged so libpq rejects it when connecting, with
    the same connection error the psycopg scripts report.  Raises
    psycopg.ProgrammingError if the settings cannot be parsed at all.

    It lives here, next to the engine, rather than in db_config.py, so the db
    package (shared with the web image) never needs SQLAlchemy.
    """
    params = dict(conninfo_to_dict(get_database_url()))
    user = params.pop("user", None)
    password = params.pop("password", None)
    host = params.pop("host", None)
    dbname = params.pop("dbname", None)
    port = str(params.pop("port", "") or "")
    if port and not port.isdigit():
        params["port"] = port
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=int(port) if port.isdigit() else None,
        database=dbname,
        query=params,
    )


# A declarative base only carries SQLAlchemy's mapping machinery; it needs no public methods.
class Base(DeclarativeBase):  # pylint: disable=too-few-public-methods
    """Declarative base class for this project's ORM models."""


# A mapped class describes table columns; its behaviour comes from the SQLAlchemy Session.
class Applicant(Base):  # pylint: disable=too-few-public-methods
    """One Grad Café admissions entry (a row of the applicants table)."""

    __tablename__ = TABLE_NAME

    p_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    program: Mapped[str | None] = mapped_column(Text)
    comments: Mapped[str | None] = mapped_column(Text)
    date_added: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    term: Mapped[str | None] = mapped_column(Text)
    us_or_international: Mapped[str | None] = mapped_column(Text)
    gpa: Mapped[float | None] = mapped_column(Float)
    gre: Mapped[float | None] = mapped_column(Float)
    gre_v: Mapped[float | None] = mapped_column(Float)
    gre_aw: Mapped[float | None] = mapped_column(Float)
    degree: Mapped[str | None] = mapped_column(Text)
    llm_generated_program: Mapped[str | None] = mapped_column(Text)
    llm_generated_university: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (f"<Applicant p_id={self.p_id} program={self.program!r} "
                f"status={self.status!r} term={self.term!r}>")


# connect_timeout makes an unreachable host fail after 10 s (the same limit the
# psycopg scripts use) instead of waiting for the operating system's TCP timeout.
try:
    engine = create_engine(
        get_sqlalchemy_url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
except (psycopg.ProgrammingError, ValueError, ArgumentError) as error:
    # Settings that cannot be parsed (bad escape, non-numeric port, ...): say so, never echo them.
    raise SystemExit(INVALID_SETTINGS_MESSAGE) from error
# The usual SQLAlchemy name for a session factory; callers import it as SessionLocal.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)  # pylint: disable=invalid-name
