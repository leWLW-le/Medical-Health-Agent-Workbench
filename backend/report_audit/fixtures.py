from datetime import date
from decimal import Decimal

from .models import LabCode, Observation


def observation(
    record_id: str, patient_id: str, encounter_id: str, collected_on: date,
    code: LabCode, value: str, unit: str, revision: int = 1,
) -> Observation:
    return Observation(
        id=record_id, patient_id=patient_id, encounter_id=encounter_id,
        collected_on=collected_on, code=code, value=Decimal(value), unit=unit,
        revision=revision,
    )


CURRENT_DATE = date(2026, 8, 12)
PREVIOUS_DATE = date(2026, 6, 15)
SAMPLE_PATIENT = "DEMO-001"
SAMPLE_ENCOUNTER = "VISIT-002"

# Same-day observation for the second patient deliberately overlaps with the draft value.
# A lookup by test and date alone would incorrectly mark the draft as matching.
OBSERVATIONS = (
    observation("OBS-101", SAMPLE_PATIENT, SAMPLE_ENCOUNTER, CURRENT_DATE, "HGB", "132", "g/L", 2),
    observation("OBS-102", SAMPLE_PATIENT, SAMPLE_ENCOUNTER, CURRENT_DATE, "WBC", "6.4", "10^9/L"),
    observation("OBS-103", SAMPLE_PATIENT, SAMPLE_ENCOUNTER, CURRENT_DATE, "PLT", "245", "10^9/L"),
    observation("OBS-081", SAMPLE_PATIENT, "VISIT-001", PREVIOUS_DATE, "HGB", "126", "g/L"),
    observation("OBS-082", SAMPLE_PATIENT, "VISIT-001", PREVIOUS_DATE, "WBC", "5.8", "10^9/L"),
    observation("OBS-083", SAMPLE_PATIENT, "VISIT-001", PREVIOUS_DATE, "PLT", "232", "10^9/L"),
    observation("OBS-201", "DEMO-002", "VISIT-003", CURRENT_DATE, "HGB", "123", "g/L"),
    observation("OBS-202", "DEMO-002", "VISIT-003", CURRENT_DATE, "RBC", "4.52", "10^12/L"),
    observation("OBS-203", "DEMO-002", "VISIT-003", CURRENT_DATE, "HCT", "42.1", "%"),
    observation("OBS-204", "DEMO-002", "VISIT-003", CURRENT_DATE, "MCV", "91.3", "fL"),
    observation("OBS-205", "DEMO-002", "VISIT-003", CURRENT_DATE, "AST", "21", "U/L"),
    observation("OBS-206", "DEMO-002", "VISIT-003", CURRENT_DATE, "CREA", "83", "μmol/L"),
)

DEMO_REPORT = """PATIENT DEMO-001
ENCOUNTER VISIT-002
HGB 123 g/L 2026-08-12
WBC 6.4 10^9/L 2026-08-12
PLT 245 10^9/L 2026-08-12
"""


class SyntheticEvidenceSource:
    def __init__(self, records: tuple[Observation, ...]) -> None:
        latest: dict[tuple[str, str, str], Observation] = {}
        for item in records:
            key = (item.patient_id, item.encounter_id, item.id)
            if key not in latest or item.revision > latest[key].revision:
                latest[key] = item
        self.records = tuple(latest.values())

    def encounter_records(self, patient_id: str, encounter_id: str) -> tuple[Observation, ...] | None:
        matches = tuple(
            item for item in self.records
            if item.patient_id == patient_id and item.encounter_id == encounter_id
        )
        return matches or None

    def prior_records(
        self, patient_id: str, code: LabCode, before: date, *, exclude_encounter: str,
    ) -> tuple[Observation, ...]:
        return tuple(
            item for item in self.records
            if item.patient_id == patient_id and item.encounter_id != exclude_encounter
            and item.code == code and item.collected_on < before
        )


demo_evidence = SyntheticEvidenceSource(OBSERVATIONS)
