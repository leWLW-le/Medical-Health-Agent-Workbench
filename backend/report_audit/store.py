from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from .evidence import EvidenceContext, EvidenceSource
from .models import Draft, Snapshot
from .workflow import audit_graph

DEFAULT_DB = Path(__file__).resolve().parents[1] / "report_audit.sqlite3"


class UnknownCase(Exception):
    pass


class VersionConflict(Exception):
    pass


class IdentityConflict(Exception):
    pass


@contextmanager
def open_db() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(os.getenv("REPORT_AUDIT_DB", str(DEFAULT_DB)), timeout=10)
    try:
        with db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS audit_versions (
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (case_id, version)
                )"""
            )
            yield db
    finally:
        db.close()


def read_snapshot(db: sqlite3.Connection, case_id: str, version: int | None = None) -> Snapshot:
    if version is None:
        row = db.execute(
            "SELECT snapshot FROM audit_versions WHERE case_id = ? ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT snapshot FROM audit_versions WHERE case_id = ? AND version = ?", (case_id, version),
        ).fetchone()
    if row is None:
        raise UnknownCase
    return Snapshot.model_validate_json(row[0])


def write_snapshot(
    db: sqlite3.Connection, case_id: str, draft: Draft, previous: Snapshot | None = None,
    *, source: EvidenceSource | None = None,
) -> Snapshot:
    context = EvidenceContext(source) if source is not None else None
    result = audit_graph.invoke({"draft": draft, "previous": previous}, context=context)["result"]
    snapshot = Snapshot(
        case_id=case_id, version=previous.version + 1 if previous else 1,
        draft=draft, **result.model_dump(),
    )
    db.execute(
        "INSERT INTO audit_versions (case_id, version, snapshot) VALUES (?, ?, ?)",
        (case_id, snapshot.version, snapshot.model_dump_json()),
    )
    return snapshot


def create_case(draft: Draft, *, source: EvidenceSource | None = None) -> Snapshot:
    with open_db() as db:
        return write_snapshot(db, str(uuid4()), draft, source=source)


def get_snapshot(case_id: str, version: int | None = None) -> Snapshot:
    with open_db() as db:
        return read_snapshot(db, case_id, version)


def revise_case(
    case_id: str, expected_version: int, draft: Draft, *, source: EvidenceSource | None = None,
) -> Snapshot:
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = read_snapshot(db, case_id)
        if expected_version != previous.version:
            raise VersionConflict
        if (draft.patient_id, draft.encounter_id) != (previous.draft.patient_id, previous.draft.encounter_id):
            raise IdentityConflict
        return write_snapshot(db, case_id, draft, previous, source=source)
