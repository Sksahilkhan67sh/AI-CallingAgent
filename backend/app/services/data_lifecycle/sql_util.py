"""Small typing helper for the data-lifecycle DML statements."""

from typing import Any, cast

from sqlalchemy.engine import CursorResult, Result


def rowcount(result: Result[Any]) -> int:
    """Rows matched by an UPDATE/DELETE.

    At runtime ``Session.execute()`` on a DML statement returns a ``CursorResult``;
    SQLAlchemy 2.1's stubs type it as the more general ``Result``, which has no ``rowcount``.
    """
    return cast("CursorResult[Any]", result).rowcount
