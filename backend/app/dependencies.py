"""Shared FastAPI dependencies."""

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.core.database import get_db

__all__ = ["get_db", "Session", "Generator"]
