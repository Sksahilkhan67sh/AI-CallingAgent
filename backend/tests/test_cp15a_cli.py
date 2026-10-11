"""CP15A -- the three command-line entrypoints (run against the test transaction)."""

import contextlib

import pytest

from app.core.config import get_settings
from app.scripts import erase_data, reapply_erasures, run_retention
from app.services.data_lifecycle.retention import advisory_lock
from tests.conftest import test_engine
from tests.cp15_helpers import (
    add_transcript,
    make_attempt,
    make_campaign,
    make_contact,
    next_number,
)


@pytest.fixture
def cli(db_session, monkeypatch):
    for module in (run_retention, erase_data, reapply_erasures):
        monkeypatch.setattr(module, "SessionLocal", lambda: contextlib.nullcontext(db_session))
    monkeypatch.setattr(run_retention, "engine", test_engine)
    return db_session


def test_run_retention_defaults_to_dry_run(cli, monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "retention_transcript_days", 1)
    add_transcript(
        cli, make_attempt(cli, make_contact(cli, make_campaign(cli))), age_days=60
    )
    assert run_retention.main([]) == 0
    out = capsys.readouterr().out
    assert "dry_run" in out and "transcript_messages: 3" in out
    from sqlalchemy import func, select

    from app.models.conversation import ConversationMessage

    assert cli.execute(select(func.count()).select_from(ConversationMessage)).scalar_one() == 3
    assert run_retention.main(["--apply"]) == 0
    assert cli.execute(select(func.count()).select_from(ConversationMessage)).scalar_one() == 0


def test_run_retention_exits_3_while_another_run_holds_the_lock(cli, capsys):
    with advisory_lock(test_engine) as held:
        assert held
        assert run_retention.main(["--apply"]) == 3
    assert "holds the lock" in capsys.readouterr().err


def test_erase_cli_never_prints_the_number_and_reports_holds(cli, capsys):
    number = next_number()
    make_contact(cli, make_campaign(cli), number)
    assert erase_data.main(["--phone", number, "--reason", "subject request"]) == 0
    out = capsys.readouterr().out
    assert number not in out and number[-4:] in out
    held = make_contact(cli, make_campaign(cli, hold=True), next_number())
    assert erase_data.main(["--contact-id", str(held.id), "--reason", "subject request"]) == 2


def test_reapply_cli_is_a_dry_run_unless_asked(cli, capsys):
    assert reapply_erasures.main([]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert reapply_erasures.main(["--apply"]) == 0
    assert "APPLIED" in capsys.readouterr().out
