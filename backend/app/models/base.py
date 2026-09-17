"""Declarative base shared by all ORM models."""

from sqlalchemy import Enum
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def pg_enum(enum_cls, name: str) -> Enum:
    """SQLAlchemy Enum column that persists the enum's *value* (the
    canonical spec string, e.g. "Pending") rather than its Python member
    name (e.g. "PENDING"), which is SQLAlchemy's Enum() default.
    """
    return Enum(enum_cls, name=name, values_callable=lambda x: [e.value for e in x])
