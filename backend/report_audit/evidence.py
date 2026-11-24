from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .models import LabCode, Observation


class EvidenceSource(Protocol):
    def encounter_records(self, patient_id: str, encounter_id: str) -> Sequence[Observation] | None:
        """Return current records, including repeat draws. None means the encounter is unknown."""
        ...

    def prior_records(
        self, patient_id: str, code: LabCode, before: date, *, exclude_encounter: str,
    ) -> Sequence[Observation]:
        """Return current revisions for this test from earlier encounters of the same patient."""
        ...


@dataclass(frozen=True)
class EvidenceContext:
    source: EvidenceSource


class UnknownEncounter(Exception):
    pass


def get_evidence_source() -> EvidenceSource:
    from .fixtures import demo_evidence

    return demo_evidence


def load_encounter_records(
    source: EvidenceSource, patient_id: str, encounter_id: str,
) -> list[Observation]:
    records = source.encounter_records(patient_id, encounter_id)
    if records is None:
        raise UnknownEncounter
    return [
        record for record in records
        if record.patient_id == patient_id and record.encounter_id == encounter_id
    ]
