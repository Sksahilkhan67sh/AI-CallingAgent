"""The real API plus one route that holds a database connection for a while (P2-7 gate)."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import DB_SESSION
from app.main import create_app

app = create_app()


@app.get("/_db_slow")
def db_slow(seconds: float = 3.0, db: Session = DB_SESSION) -> dict[str, bool]:
    db.execute(text("select pg_sleep(:s)"), {"s": seconds})
    return {"ok": True}
