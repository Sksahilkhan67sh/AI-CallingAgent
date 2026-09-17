"""Generic pagination primitives shared by every list endpoint.

Limit/offset, not cursor-based -- see docs/CHECKPOINT-02-NOTES.md.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


class PaginationParams(BaseModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
