"""P2-9: Phase 2 is closed out honestly: every new setting is documented, nothing is left open,
the final report exists and the README says where things stand.

Static checks of repository files; no database, no clock, no randomness."""

import re
from pathlib import Path

from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
P2 = REPO / "docs" / "hardening-p2"
ENV_EXAMPLE = (REPO / "backend" / ".env.example").read_text()
SETTINGS_DOC = (P2 / "SETTINGS.md").read_text()
# Phase 2 settings are the db_*, web_* and api_threadpool_* fields; RUN_MIGRATIONS is read by the
# container entrypoint, not by Settings.
PHASE2_PREFIXES = ("db_", "web_", "api_threadpool")


def _phase2_env_names() -> list[str]:
    names = [n.upper() for n in Settings.model_fields if n.startswith(PHASE2_PREFIXES)]
    return sorted([*names, "RUN_MIGRATIONS"])


def test_the_phase_2_settings_are_found() -> None:
    names = _phase2_env_names()
    # A guard against the prefix rule silently matching nothing.
    assert len(names) >= 20
    assert {"DB_POOL_SIZE", "DB_STATEMENT_TIMEOUT_MS", "WEB_CONCURRENCY"} <= set(names)


def test_every_phase_2_setting_is_in_env_example() -> None:
    missing = [
        n for n in _phase2_env_names() if not re.search(rf"^#?\s*{n}=", ENV_EXAMPLE, re.MULTILINE)
    ]
    assert missing == [], f"not in backend/.env.example: {missing}"


def test_every_phase_2_setting_is_in_settings_md() -> None:
    missing = [n for n in _phase2_env_names() if f"`{n}`" not in SETTINGS_DOC]
    assert missing == [], f"not in docs/hardening-p2/SETTINGS.md: {missing}"


def _state_rows() -> list[list[str]]:
    rows = []
    for line in (P2 / "STATE.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| P2-") and len(cells) >= 4:
            rows.append(cells)
    return rows


def test_every_task_in_state_is_merged_or_blocked() -> None:
    rows = _state_rows()
    assert [r[0] for r in rows] == [f"P2-{i}" for i in range(10)]
    open_rows = {r[0]: r[2] for r in rows if r[2] not in ("MERGED", "BLOCKED")}
    assert open_rows == {}, open_rows
    assert all(re.fullmatch(r"#\d+", r[3]) for r in rows if r[2] == "MERGED")


def test_the_final_report_exists_and_states_the_completion_condition() -> None:
    report = (P2 / "FINAL-REPORT.md").read_text()
    for needle in ("P2-0", "P2-9", "HUMAN-TODO", "Remaining risks", "mutation"):
        assert needle in report, needle
    assert "Phase 2 is complete only when P2-7 passes in CI on main" in report


def test_the_readme_status_names_phase_2_and_the_real_gate_files() -> None:
    readme = (REPO / "README.md").read_text()
    assert "Phase 2" in readme and "docs/hardening-p2/FINAL-REPORT.md" in readme
    assert "test_p2_db_resilience.py" in readme
    for path in re.findall(r"`(backend/tests/[\w./]+\.py)`", readme):
        assert (REPO / path).exists(), f"README names a test file that does not exist: {path}"
