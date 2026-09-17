"""Database connectivity and migration state checks."""

from sqlalchemy import inspect, text


def test_database_connection_works(db_session):
    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_migrations_created_all_expected_tables(db_session):
    inspector = inspect(db_session.get_bind())
    tables = set(inspector.get_table_names())

    expected = {
        "campaign",
        "retry_policy",
        "contact",
        "call_attempt",
        "suppression",
        "audit_log",
        "processed_event",
        "conversation_session",
        "conversation_message",
        "call_event",
    }
    assert expected.issubset(tables)
