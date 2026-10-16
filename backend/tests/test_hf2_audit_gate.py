"""HF2 2.3 -- the CI dependency-audit gate (ops/ci/audit_gate.py)."""

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "ci" / "audit_gate.py"
_spec = importlib.util.spec_from_file_location("audit_gate", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

TODAY = date(2026, 10, 16)

_TABLE = """\
intro mentions PYSEC-2099-1 in prose and must not count

## Active exceptions

| Advisory | Package | Why | Owner | Review by |
|---|---|---|---|---|
| PYSEC-2026-1 | libfoo | not reachable | alice | 2026-12-01 |
| GHSA-aaaa-bbbb-cccc | bar | no fix | bob | 2026-10-01 |
| CVE-2026-9 | baz | upstream disputes | carol | soon |
| PYSEC-2026-2 | qux | | dave | 2026-12-01 |

## Known, not gated

| PYSEC-2026-3 | dev-only | not read | eve | 2027-01-01 |
"""


def test_exceptions_table_parsing():
    allowed, problems = gate.load_exceptions(_TABLE, TODAY)
    assert allowed == {"PYSEC-2026-1"}  # prose, other sections, expired and bad rows excluded
    joined = "\n".join(problems)
    assert "GHSA-aaaa-bbbb-cccc: exception expired" in joined
    assert "CVE-2026-9" in joined and "not YYYY-MM-DD" in joined
    assert "PYSEC-2026-2: needs package, reason, owner" in joined


def test_the_real_exceptions_file_is_well_formed():
    allowed, problems = gate.load_exceptions(gate.EXCEPTIONS_FILE.read_text(), date.today())
    assert problems == []
    assert allowed == set()  # HF2 leaves nothing excepted; adding one is a reviewed change


def _dep(vulns):
    return {"name": "pkg", "version": "1.0", "vulns": vulns}


def test_pip_findings_fail_unless_the_id_or_an_alias_is_excepted():
    finding = _dep([{"id": "PYSEC-2026-1", "aliases": ["CVE-2026-100"]}])
    assert gate.evaluate_pip([finding], set()) == ["pkg==1.0: PYSEC-2026-1"]
    assert gate.evaluate_pip([finding], {"PYSEC-2026-1"}) == []
    assert gate.evaluate_pip([finding], {"CVE-2026-100"}) == []
    assert gate.evaluate_pip([{"name": "ok", "version": "1", "vulns": []}], set()) == []


def _npm(severity, url):
    return {
        "vulnerabilities": {
            "next": {"severity": severity, "via": [{"url": url, "title": "t"}]},
            "inherits": {"severity": "high", "via": ["next"]},
        }
    }


_URL = "https://github.com/advisories/GHSA-vcvr-r3jv-pc5j"


def test_npm_high_and_critical_fail_unless_excepted():
    assert gate.evaluate_npm(_npm("critical", _URL), set()) == [
        "next (critical): GHSA-vcvr-r3jv-pc5j"
    ]
    assert gate.evaluate_npm(_npm("high", _URL), set())  # high counts
    assert gate.evaluate_npm(_npm("high", _URL), {"GHSA-vcvr-r3jv-pc5j"}) == []


def test_npm_moderate_and_inherited_only_entries_do_not_fail():
    assert gate.evaluate_npm(_npm("moderate", _URL), set()) == []  # `inherits` has no advisory
    assert gate.evaluate_npm({"vulnerabilities": {}}, set()) == []


def _fake_run(stdout, returncode=0):
    return lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout=stdout, stderr="")


def test_main_pip_exit_codes(monkeypatch):
    report = {"dependencies": [_dep([{"id": "PYSEC-2026-1", "aliases": []}])]}
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(json.dumps(report), 1))
    assert gate.main(["pip"]) == 1
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(json.dumps({"dependencies": []})))
    assert gate.main(["pip"]) == 0
    monkeypatch.setattr(gate.subprocess, "run", _fake_run("", 3))  # the tool itself broke
    assert gate.main(["pip"]) == 2


def test_main_npm_exit_codes(monkeypatch):
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(json.dumps(_npm("high", _URL)), 1))
    assert gate.main(["npm"]) == 1
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(json.dumps({"vulnerabilities": {}})))
    assert gate.main(["npm"]) == 0
    monkeypatch.setattr(gate.subprocess, "run", _fake_run("not json"))
    assert gate.main(["npm"]) == 2
    monkeypatch.setattr(gate.subprocess, "run", _fake_run(json.dumps({"error": {"code": "x"}})))
    assert gate.main(["npm"]) == 2


@pytest.mark.skipif(
    subprocess.run(["which", "pip-audit"], capture_output=True).returncode != 0,
    reason="pip-audit not installed",
)
def test_gate_runs_end_to_end_against_the_real_constraints_file():
    result = subprocess.run(
        ["python", str(_SCRIPT), "pip", "--requirements", "constraints.txt"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
