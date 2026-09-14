"""
models.py - SQLAlchemy 2.x ORM mapping of the existing `applicants` table.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

The Applicant class maps onto the very table load_data.py creates and fills;
SQLAlchemy never creates a second copy of the data (no create_all() here).

Engine and Session:
  * `engine` is created once from the same environment settings psycopg uses
    (db_config.get_sqlalchemy_url() selects the psycopg 3 driver).  Creating
    an engine does not open a connection; connections are pooled and checked
    with pool_pre_ping so a restarted database does not break the web app.
  * `SessionLocal` is a sessionmaker; use it as a context manager:

        from models import Applicant, SessionLocal
        with SessionLocal() as session:
            first = session.get(Applicant, 1020481)
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, Integer, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from db_config import TABLE_NAME, get_sqlalchemy_url


class Base(DeclarativeBase):
    """Declarative base class for this project's ORM models."""


class Applicant(Base):
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
        return f"<Applicant p_id={self.p_id} program={self.program!r} status={self.status!r} term={self.term!r}>"


engine = create_engine(get_sqlalchemy_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
