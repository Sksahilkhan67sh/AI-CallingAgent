"""Drill F: freshness exit codes and GFS prune selection, with INJECTED time (NOW_OVERRIDE) on
synthetic dated manifests. No database, no encryption tools needed."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ops.helpers import OPS

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _stamp(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


def make_backup(dest: Path, kind: str, when: datetime, start_wal: str | None = None) -> Path:
    d = dest / kind
    d.mkdir(parents=True, exist_ok=True)
    art = d / f"{kind}-{_stamp(when)}.{'dump' if kind == 'logical' else 'tar.gz'}.age"
    art.write_bytes(b"age-encryption.org/v1 synthetic")
    manifest = {"kind": kind, "created_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if start_wal:
        manifest["start_wal_segment"] = start_wal
    Path(f"{art}.manifest.json").write_text(json.dumps(manifest))
    return art


def make_wal(dest: Path, name: str, when: datetime) -> None:
    wal = dest / "wal"
    wal.mkdir(parents=True, exist_ok=True)
    for suffix in (".age", ".sha256"):
        p = wal / f"{name}{suffix}"
        p.write_bytes(b"x")
        os.utime(p, (when.timestamp(), when.timestamp()))


def seg(n: int, tl: int = 1) -> str:
    return f"{tl:08X}{n // 256:08X}{n % 256:08X}"


def run(
    script: str, dest: Path, *args: str, now: datetime = NOW, **env
) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        "BACKUP_DEST": str(dest),
        "BACKUP_ENV_FILE": "/nonexistent",
        "NOW_OVERRIDE": now.isoformat(),
        **env,
    }
    return subprocess.run(
        [str(OPS / script), *args], env=full, capture_output=True, text=True, check=False
    )  # noqa: S603


@pytest.fixture
def healthy(tmp_path):
    dest = tmp_path / "d"
    make_backup(dest, "logical", NOW - timedelta(hours=1))
    make_backup(dest, "base", NOW - timedelta(days=1), start_wal=seg(1))
    for n in (1, 2, 3):
        make_wal(dest, seg(n), NOW - timedelta(minutes=3))
    return dest


def _status(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_healthy_exits_0_with_machine_readable_json(healthy):
    r = run("check_backup_freshness.sh", healthy)
    assert r.returncode == 0, r.stderr
    body = _status(r)
    assert body["status"] == "ok" and body["exit_code"] == 0
    assert set(body) >= {"logical", "base", "wal", "wal_gaps"}


@pytest.mark.parametrize("hours,code", [(25, 0), (26, 1), (49, 1), (50, 2), (200, 2)])
def test_logical_backup_age_thresholds(healthy, hours, code):
    for f in (healthy / "logical").iterdir():
        f.unlink()
    make_backup(healthy, "logical", NOW - timedelta(hours=hours))
    assert run("check_backup_freshness.sh", healthy).returncode == code


@pytest.mark.parametrize("hours,code", [(199, 0), (200, 1), (379, 1), (380, 2)])
def test_base_backup_age_thresholds(healthy, hours, code):
    for f in (healthy / "base").iterdir():
        f.unlink()
    make_backup(healthy, "base", NOW - timedelta(hours=hours), start_wal=seg(1))
    assert run("check_backup_freshness.sh", healthy).returncode == code


@pytest.mark.parametrize("minutes,code", [(14, 0), (15, 1), (29, 1), (30, 2)])
def test_wal_age_is_the_rpo_signal(healthy, minutes, code):
    for f in (healthy / "wal").iterdir():
        f.unlink()
    for n in (1, 2, 3):
        make_wal(healthy, seg(n), NOW - timedelta(minutes=minutes))
    assert run("check_backup_freshness.sh", healthy).returncode == code


def test_nothing_at_all_is_critical(tmp_path):
    r = run("check_backup_freshness.sh", tmp_path / "empty")
    assert r.returncode == 2 and _status(r)["logical"]["status"] == "critical"


@pytest.mark.parametrize("breakage", ["missing", "invalid"])
def test_a_missing_or_invalid_manifest_is_critical(healthy, breakage):
    manifest = next((healthy / "logical").glob("*.manifest.json"))
    manifest.unlink() if breakage == "missing" else manifest.write_text("{not json")
    r = run("check_backup_freshness.sh", healthy)
    assert r.returncode == 2 and "manifest" in _status(r)["logical"]["reason"]


def test_a_gap_in_the_wal_sequence_is_critical_and_counted(healthy):
    for f in (healthy / "wal").iterdir():
        f.unlink()
    for n in (1, 2, 3, 6):  # 4 and 5 are missing
        make_wal(healthy, seg(n), NOW - timedelta(minutes=2))
    r = run("check_backup_freshness.sh", healthy)
    assert r.returncode == 2
    assert _status(r)["wal_gaps"] == {"status": "critical", "missing_segments": 2}


def test_a_new_timeline_is_not_reported_as_a_gap(healthy):
    for n in (4, 5):
        make_wal(healthy, seg(n), NOW - timedelta(minutes=2))
    make_wal(healthy, seg(5, tl=2), NOW - timedelta(minutes=1))  # promotion: TL2 starts at 5
    assert run("check_backup_freshness.sh", healthy).returncode == 0


def test_wal_gap_detection_crosses_a_log_file_boundary(healthy):
    for f in (healthy / "wal").iterdir():
        f.unlink()
    for n in (254, 255, 256, 257):  # 0xFF -> next 4 GiB log file: contiguous
        make_wal(healthy, seg(n), NOW - timedelta(minutes=2))
    assert run("check_backup_freshness.sh", healthy).returncode == 0


# ---- prune ---------------------------------------------------------------------------------


def reference_keep(times: list[datetime], newest=3, daily=7, weekly=4, monthly=12) -> set[datetime]:
    """Independent implementation of the documented policy (written from the spec)."""
    keep: set[datetime] = set(sorted(times, reverse=True)[:newest])
    for keyfn, limit in (
        (lambda t: t.date(), daily),
        (lambda t: t.isocalendar()[:2], weekly),
        (lambda t: (t.year, t.month), monthly),
    ):
        seen: list = []
        for t in sorted(times, reverse=True):
            k = keyfn(t)
            if k not in seen and len(seen) < limit:
                seen.append(k)
                keep.add(t)
    return keep


@pytest.fixture
def aged(tmp_path):
    """Two logical backups per day for 400 days (so 'newest of the day' matters), plus fresh
    base backups / WAL so the freshness gate passes."""
    dest = tmp_path / "d"
    times = []
    for day in range(400):
        for hour in (2, 14):
            t = (NOW - timedelta(days=day)).replace(hour=hour, minute=0, second=0)
            if t <= NOW - timedelta(hours=1):
                make_backup(dest, "logical", t)
                times.append(t)
    make_backup(dest, "base", NOW - timedelta(days=1), start_wal=seg(10))
    for n in (10, 11):
        make_wal(dest, seg(n), NOW - timedelta(minutes=2))
    return dest, times


def _kept_logical(dest):
    return {
        datetime.strptime(p.name.split("-")[1][:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        for p in (dest / "logical").iterdir()
        if not p.name.endswith(".json")
    }


def test_dry_run_selects_per_policy_and_deletes_nothing(aged):
    dest, times = aged
    before = _kept_logical(dest)
    r = run("prune_backups.sh", dest)
    assert r.returncode == 0, r.stderr
    assert _kept_logical(dest) == before  # nothing deleted
    assert "mode=dry-run" in r.stdout
    kept = {line.split()[1] for line in r.stdout.splitlines() if line.startswith("KEEP   logical/")}
    expected = {f"logical/logical-{_stamp(t)}.dump.age" for t in reference_keep(times)}
    assert kept == expected


def test_apply_deletes_exactly_the_complement_with_their_manifests(aged):
    dest, times = aged
    r = run("prune_backups.sh", dest, "--apply")
    assert r.returncode == 0, r.stderr
    assert _kept_logical(dest) == reference_keep(times)
    names = {p.name for p in (dest / "logical").iterdir()}
    assert all(f"{n}.manifest.json" in names for n in names if not n.endswith(".json"))
    assert sum(1 for n in names if n.endswith(".json")) == len(reference_keep(times))


def test_the_newest_n_are_never_deleted_whatever_the_policy(aged):
    dest, times = aged
    run(
        "prune_backups.sh",
        dest,
        "--apply",
        KEEP_DAILY="0",
        KEEP_WEEKLY="0",
        KEEP_MONTHLY="0",
        KEEP_NEWEST="3",
    )
    assert _kept_logical(dest) == set(sorted(times, reverse=True)[:3])


def test_prune_refuses_when_the_newest_backup_is_not_fresh(aged):
    dest, times = aged
    before = _kept_logical(dest)
    r = run("prune_backups.sh", dest, "--apply", now=NOW + timedelta(days=30))
    assert r.returncode != 0 and "not fresh" in r.stderr
    assert _kept_logical(dest) == before


def test_wal_is_deleted_only_below_the_oldest_retained_base_start(tmp_path):
    dest = tmp_path / "d"
    make_backup(dest, "logical", NOW - timedelta(hours=1))
    # two base backups; the policy keeps both (newest 3), so the OLDER start segment is the bound
    make_backup(dest, "base", NOW - timedelta(days=8), start_wal=seg(20))
    make_backup(dest, "base", NOW - timedelta(days=1), start_wal=seg(40))
    for n in range(10, 45):
        make_wal(dest, seg(n), NOW - timedelta(minutes=2))
    (dest / "wal" / "00000002.history").write_bytes(b"h")
    r = run("prune_backups.sh", dest, "--apply")
    assert r.returncode == 0, r.stderr
    left = {p.name for p in (dest / "wal").iterdir()}
    assert seg(19) + ".age" not in left and seg(10) + ".sha256" not in left  # older than 20: gone
    assert seg(20) + ".age" in left and seg(44) + ".age" in left  # >= the oldest retained start
    assert "00000002.history" in left


def test_no_base_backup_means_no_wal_is_deleted(tmp_path):
    dest = tmp_path / "d"
    make_backup(dest, "logical", NOW - timedelta(hours=1))
    for n in (1, 2, 3):
        make_wal(dest, seg(n), NOW - timedelta(minutes=2))
    make_backup(dest, "base", NOW - timedelta(hours=1))  # fresh but manifest has no start segment
    r = run("prune_backups.sh", dest, "--apply")
    assert r.returncode == 0
    assert len(list((dest / "wal").iterdir())) == 6


def test_gaps_only_needs_just_the_wal_directory_and_reports_the_gap(tmp_path):
    wal_dest = tmp_path / "d"
    for n in (1, 2, 3):
        make_wal(wal_dest, seg(n), NOW)
    env = {"WAL_ARCHIVE_DIR": str(wal_dest / "wal")}
    ok = subprocess.run(  # noqa: S603
        [str(OPS / "check_backup_freshness.sh"), "--gaps-only"],
        env={**os.environ, **env, "BACKUP_ENV_FILE": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0 and json.loads(ok.stdout)["wal_gaps"]["missing_segments"] == 0
    for suffix in (".age", ".sha256"):
        (wal_dest / "wal" / f"{seg(2)}{suffix}").unlink()
    bad = subprocess.run(  # noqa: S603
        [str(OPS / "check_backup_freshness.sh"), "--gaps-only"],
        env={**os.environ, **env, "BACKUP_ENV_FILE": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2 and json.loads(bad.stdout)["wal_gaps"]["missing_segments"] == 1


def test_gaps_only_without_a_wal_directory_is_an_error(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "WAL_ARCHIVE_DIR"}
    r = subprocess.run(  # noqa: S603
        [str(OPS / "check_backup_freshness.sh"), "--gaps-only"],
        env={**env, "BACKUP_ENV_FILE": "/nonexistent"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0 and "WAL_ARCHIVE_DIR" in r.stderr
