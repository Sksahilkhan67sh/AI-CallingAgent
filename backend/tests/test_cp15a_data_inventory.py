"""CP15A -- the classification guard. Fails when a column is unclassified (protects every
future checkpoint, including new analysis/recording columns), when the registry names a
column that no longer exists, or when a PII column has no purge/never-purge retention."""

from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table

import app.models  # noqa: F401  (registers every table)
from app.core import data_inventory as inv
from app.models.base import Base

DOC = Path(__file__).resolve().parents[2] / "docs" / "DATA-INVENTORY.md"


def test_registry_matches_the_real_schema_exactly():
    assert inv.registry_problems(Base.metadata) == []


def test_an_unclassified_column_fails():
    md = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(md)
    Table("future_table", md, Column("id", String, primary_key=True), Column("notes", String))
    problems = inv.registry_problems(md)
    assert "unclassified column: future_table.notes" in problems
    assert "unclassified column: future_table.id" in problems


def test_a_new_column_on_an_existing_table_fails():
    md = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(md)
    md.tables["call_analysis"].append_column(Column("new_free_text", String))
    assert "unclassified column: call_analysis.new_free_text" in inv.registry_problems(md)


def test_a_stale_registry_entry_fails():
    md = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name != "agent_config":
            table.to_metadata(md)
    problems = inv.registry_problems(md)
    assert "stale registry entry: agent_config.goal" in problems


def test_every_pii_direct_or_content_column_has_a_purge_or_never_purge_class():
    for key, entry in inv.REGISTRY.items():
        if entry.data_class in inv.PII_CLASSES_REQUIRING_PURGE:
            assert (
                entry.retention in inv.PURGE_CLASSES
                or entry.retention == inv.Retention.SUPPRESSION_NEVER
            ), key


def test_a_pii_column_with_a_keep_class_is_reported():
    saved = inv.REGISTRY["contact.phone_number"]
    inv.REGISTRY["contact.phone_number"] = inv.Entry(
        inv.DataClass.PII_DIRECT, inv.Retention.OPERATIONAL
    )
    try:
        assert any("contact.phone_number" in p for p in inv.registry_problems(Base.metadata))
    finally:
        inv.REGISTRY["contact.phone_number"] = saved


def test_every_purge_class_names_a_real_setting():
    from app.core.config import Settings

    fields = {name.upper() for name in Settings.model_fields}
    for retention in inv.PURGE_CLASSES:
        assert inv.RETENTION_SETTING[retention] in fields


def test_phone_numbers_and_transcript_text_are_classified_as_such():
    assert inv.REGISTRY["contact.phone_number"].data_class == inv.DataClass.PII_DIRECT
    assert inv.REGISTRY["suppression.phone_number"].data_class == inv.DataClass.PII_DIRECT
    assert inv.REGISTRY["conversation_message.content"].data_class == inv.DataClass.PII_CONTENT
    for col in ("summary", "feedback", "key_facts", "objections", "customer_needs"):
        assert inv.REGISTRY[f"call_analysis.{col}"].data_class == inv.DataClass.PII_CONTENT


def test_the_inventory_document_embeds_the_current_registry():
    text = DOC.read_text()
    assert inv.render_inventory_markdown() in text, (
        "docs/DATA-INVENTORY.md is out of date: regenerate the table with "
        "`python -c 'from app.core.data_inventory import render_inventory_markdown as r; "
        "print(r())'`"
    )
