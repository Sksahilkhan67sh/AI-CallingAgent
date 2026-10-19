"""P2-1: the container base images match what CI actually runs, Dependabot cannot drift them, CI
installs `age`, and a skip budget fails the build when the ops tests silently skip again.

Static checks of repository files plus one hermetic subprocess test of the budget script: no
database, no clock, no randomness."""

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
DEPENDABOT = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
SCRIPT = BACKEND / "scripts" / "ci_skip_budget.py"


def _from_lines(dockerfile: Path) -> list[str]:
    return re.findall(r"^FROM\s+(\S+)", dockerfile.read_text(), flags=re.MULTILINE)


def _step_with(job: str, key: str) -> list[dict]:
    return [s for s in CI["jobs"][job]["steps"] if key in s.get("with", {})]


def test_backend_image_uses_the_python_version_ci_tests() -> None:
    ci_python = _step_with("backend", "python-version")[0]["with"]["python-version"]
    assert _from_lines(BACKEND / "Dockerfile") == [f"python:{ci_python}-slim"]


def test_frontend_image_uses_the_node_major_ci_tests_and_it_is_lts() -> None:
    ci_node = str(_step_with("frontend", "node-version")[0]["with"]["node-version"])
    assert set(_from_lines(REPO / "frontend" / "Dockerfile")) == {f"node:{ci_node}-slim"}
    assert int(ci_node) % 2 == 0, "odd Node majors are never LTS"


def _docker_entry(directory: str) -> dict:
    return next(
        u
        for u in DEPENDABOT["updates"]
        if u["package-ecosystem"] == "docker" and u["directory"] == directory
    )


def _ignored(entry: dict, name: str) -> set[str]:
    return {
        t
        for rule in entry.get("ignore", [])
        if rule["dependency-name"] == name
        for t in rule["update-types"]
    }


def test_dependabot_cannot_bump_the_base_images() -> None:
    # Python's minor releases are its breaking releases (3.11 -> 3.14 is "minor" to semver),
    # so both are ignored for it; for Node only the major matters.
    assert {
        "version-update:semver-major",
        "version-update:semver-minor",
    } <= _ignored(_docker_entry("/backend"), "python")
    assert "version-update:semver-major" in _ignored(_docker_entry("/frontend"), "node")


def test_ci_installs_age_before_the_tests() -> None:
    steps = CI["jobs"]["backend"]["steps"]
    install = next(
        i
        for i, s in enumerate(steps)
        if "apt-get install" in s.get("run", "") and re.search(r"\bage\b", s["run"])
    )
    test = next(i for i, s in enumerate(steps) if "pytest" in s.get("run", ""))
    assert install < test


def test_ci_enforces_the_ops_skip_budget() -> None:
    runs = [s.get("run", "") for s in CI["jobs"]["backend"]["steps"]]
    assert any("--junitxml" in r for r in runs), "the test run must emit junit xml"
    assert any("ci_skip_budget.py" in r for r in runs), "no skip budget step in CI"


def _junit(tmp_path: Path, ops_skips: int, other_skips: int, ops_passes: int = 2) -> Path:
    cases = []
    for i in range(ops_passes):
        cases.append(f'<testcase classname="tests.ops.test_a" name="ok{i}"/>')
    for i in range(ops_skips):
        cases.append(
            f'<testcase classname="tests.ops.test_a" name="s{i}">'
            '<skipped type="pytest.skip" message="age is not installed"/></testcase>'
        )
    for i in range(other_skips):
        cases.append(
            f'<testcase classname="tests.test_other" name="o{i}">'
            '<skipped type="pytest.skip" message="x"/></testcase>'
        )
    xml = textwrap.dedent(
        """<?xml version="1.0"?><testsuites><testsuite name="pytest">{}</testsuite></testsuites>"""
    ).format("".join(cases))
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    return path


def _run(junit: Path, allow: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(junit), "--prefix", "tests.ops", "--allow", str(allow)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("ops_skips", "allow", "expected_exit"), [(0, 0, 0), (3, 3, 0), (4, 3, 1)])
def test_skip_budget_counts_only_ops_tests(
    tmp_path: Path, ops_skips: int, allow: int, expected_exit: int
) -> None:
    result = _run(_junit(tmp_path, ops_skips=ops_skips, other_skips=7), allow)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert f"{ops_skips} skipped" in result.stdout
    assert ("::error" in result.stdout) == (expected_exit == 1)


def test_skip_budget_fails_on_a_report_with_no_ops_tests(tmp_path: Path) -> None:
    # A run that never reached tests/ops must not pass the budget by counting zero skips.
    junit = _junit(tmp_path, ops_skips=0, other_skips=0, ops_passes=0)
    assert _run(junit, allow=0).returncode == 1
