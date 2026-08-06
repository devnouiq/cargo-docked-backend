"""Declarative base + naming convention shared by every model.

The naming convention matters for Alembic: without it, autogenerate picks
driver-default constraint names (e.g. Postgres's arbitrary `%(table)s_%(column)s_fkey`
suffixes vary in ways that produce noisy, non-deterministic migration
diffs). Fixing the pattern up front means `alembic revision --autogenerate`
output stays stable across environments/SQLAlchemy versions.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
