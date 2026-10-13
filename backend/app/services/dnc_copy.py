"""Secondary copies of the two records a restore must never lose -- CP15B.

* kind "dnc": the suppression table (normalized number, source, reason, created_at);
* kind "erasure": erasure_log fingerprints (fingerprint, created_at). The log lives in the
  SAME database that gets restored, so an erasure made after the restore point disappears
  together with its log row; without this copy `reapply_erasures` would have nothing to apply.

The suppression table is the global DNC list; losing entries added after a restore point
means calling people who opted out. This module writes an ENCRYPTED, checksummed export of it
(normalized number, source, reason, created_at -- nothing else) to a location independent of
the database backups, and reads one back for `reconcile_suppressions`.

Encryption uses the system `age` binary (no Python dependency). The export streams rows into
`age`, so no plaintext file is ever written. Reading needs the PRIVATE key (an identity FILE),
which lives only on a restore host.
"""

import csv
import hashlib
import io
import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.erasure_log import ErasureLog
from app.models.suppression import Suppression

SCRIPT_VERSION = "15b.1"
FIELDS = ("phone_number", "source", "reason", "created_at")  # kind "dnc"
ERASURE_FIELDS = ("fingerprint", "created_at")  # kind "erasure"
_KINDS = {"dnc": ("dnc", FIELDS), "erasure": ("erasure", ERASURE_FIELDS)}


class DncCopyError(RuntimeError):
    """A sanitized failure: the message never contains a number, key or path to a key."""


@dataclass(frozen=True)
class CopyRow:
    phone_number: str
    source: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class ErasureCopyRow:
    fingerprint: str
    created_at: datetime


def _recipient_fingerprint(recipients: list[str]) -> str:
    return hashlib.sha256(",".join(recipients).encode()).hexdigest()[:16]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_copy(
    db: Session,
    dest_dir: Path,
    *,
    recipients: list[str],
    now: datetime,
    env: str = "production",
    allow_unencrypted: bool = False,
    kind: str = "dnc",
) -> dict:
    """Write <kind>-<UTC>.csv.age (+ .manifest.json) into `dest_dir`; return the manifest."""
    if kind not in _KINDS:
        raise DncCopyError("unknown copy kind")
    prefix, fields = _KINDS[kind]
    if allow_unencrypted:
        raise DncCopyError("unencrypted DNC copies are not supported")
    if not recipients:
        raise DncCopyError("at least one age recipient is required")
    dest_dir.mkdir(parents=True, exist_ok=True)
    if env != "development" and dest_dir.stat().st_mode & 0o007:
        raise DncCopyError(f"destination is accessible by 'other'; refusing in {env} mode")

    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = dest_dir / f"{prefix}-{stamp}.csv.age"
    partial = dest_dir / f".{final.name}.partial.{os.getpid()}"
    if final.exists():
        raise DncCopyError(f"{final.name} already exists (refusing to overwrite)")

    cmd = ["age"]
    for recipient in recipients:
        cmd += ["-r", recipient]
    cmd += ["-o", str(partial)]
    rows = 0
    newest: datetime | None = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
        assert proc.stdin is not None
        text = io.TextIOWrapper(proc.stdin, encoding="utf-8", newline="")
        writer = csv.writer(text)
        writer.writerow(fields)
        if kind == "dnc":
            dnc_stmt = (
                select(
                    Suppression.phone_number,
                    Suppression.source,
                    Suppression.reason,
                    Suppression.requested_at,
                )
                .order_by(Suppression.phone_number)
                .execution_options(yield_per=1000)
            )
            records = (
                ([n, src.value, reason or "", ts.isoformat()], ts)
                for n, src, reason, ts in db.execute(dnc_stmt)
            )
        else:
            era_stmt = (
                select(ErasureLog.fingerprint, ErasureLog.requested_at)
                .order_by(ErasureLog.fingerprint, ErasureLog.requested_at)
                .execution_options(yield_per=1000)
            )
            records = (([fp, ts.isoformat()], ts) for fp, ts in db.execute(era_stmt))
        for record, created in records:
            writer.writerow(record)
            rows += 1
            if newest is None or created > newest:
                newest = created
        text.close()
        if proc.wait() != 0:
            raise DncCopyError("age failed to encrypt the export")
        partial.chmod(0o600)
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)

    manifest = {
        "format_version": 1,
        "kind": kind,
        "created_utc": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "script_version": SCRIPT_VERSION,
        "artifact": final.name,
        "row_count": rows,
        "newest_created_utc": newest.astimezone(UTC).isoformat() if newest else None,
        "size_bytes": final.stat().st_size,
        "sha256": _sha256(final),
        "encryption": {"tool": "age", "recipient_fingerprint": _recipient_fingerprint(recipients)},
    }
    tmp_manifest = dest_dir / f".{final.name}.manifest.partial.{os.getpid()}"
    try:
        tmp_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp_manifest.chmod(0o600)
        tmp_manifest.replace(Path(f"{final}.manifest.json"))
    finally:
        tmp_manifest.unlink(missing_ok=True)
    return manifest


def read_copy(
    copy_path: Path, identity_file: Path, kind: str = "dnc"
) -> tuple[dict, Iterator[Any]]:
    """Verify the copy against its manifest, then return (manifest, row iterator).

    Raises DncCopyError when the checksum, size or row count disagree with the manifest:
    a damaged copy must never be applied half-way."""
    manifest_path = Path(f"{copy_path}.manifest.json")
    if not manifest_path.is_file():
        raise DncCopyError("manifest next to the copy is missing")
    manifest = json.loads(manifest_path.read_text())
    if kind not in _KINDS or manifest.get("kind") != kind:
        raise DncCopyError(f"manifest is not a {kind} copy manifest")
    size_ok = copy_path.stat().st_size == manifest["size_bytes"]
    if not size_ok or _sha256(copy_path) != manifest["sha256"]:
        raise DncCopyError("CHECKSUM MISMATCH: the DNC copy is corrupt or was modified")

    result = subprocess.run(  # noqa: S603
        ["age", "-d", "-i", str(identity_file), str(copy_path)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DncCopyError("age could not decrypt the copy (wrong identity?)")
    reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != _KINDS[kind][1]:
        raise DncCopyError(f"unexpected columns in the {kind} copy")
    parsed: list[CopyRow] | list[ErasureCopyRow]
    if kind == "dnc":
        parsed = [
            CopyRow(
                phone_number=row["phone_number"],
                source=row["source"],
                reason=row["reason"] or None,
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in reader
        ]
    else:
        parsed = [
            ErasureCopyRow(
                fingerprint=row["fingerprint"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in reader
        ]
    if len(parsed) != manifest["row_count"]:
        raise DncCopyError("row count differs from the manifest (truncated copy)")
    return manifest, iter(parsed)
