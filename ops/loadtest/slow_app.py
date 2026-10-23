"""The real API plus one route that holds a database connection for a while.

Load-test only (never imported by the application). Fast queries saturate the CPU long before
they exercise the connection pool; a request that holds a connection for `ms` milliseconds
(a server-side sleep, so it costs no CPU) is what shows pool and threadpool behaviour.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import DB_SESSION
from app.main import create_app

app = create_app()


@app.get("/_db_slow")
def db_slow(ms: int = 500, db: Session = DB_SESSION) -> dict[str, bool]:
    db.execute(text("select pg_sleep(:s)"), {"s": ms / 1000})
    return {"ok": True}
