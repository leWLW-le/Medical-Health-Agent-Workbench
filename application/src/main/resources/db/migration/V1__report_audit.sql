CREATE TABLE audit_case (
    case_id VARCHAR(36) PRIMARY KEY,
    patient_id VARCHAR(32) NOT NULL,
    encounter_id VARCHAR(32) NOT NULL,
    submitted_version INT NOT NULL,
    completed_version INT NOT NULL DEFAULT 0
);

CREATE TABLE audit_job (
    job_id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL,
    version INT NOT NULL,
    base_version INT NOT NULL,
    status VARCHAR(12) NOT NULL,
    draft TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    available_at BIGINT NOT NULL,
    lease_until BIGINT,
    claim_token VARCHAR(36),
    error_code VARCHAR(64),
    FOREIGN KEY (case_id) REFERENCES audit_case(case_id),
    UNIQUE (case_id, version)
);

CREATE INDEX audit_job_pending ON audit_job(status, available_at, job_id);
CREATE INDEX audit_job_leases ON audit_job(status, lease_until, job_id);

CREATE TABLE audit_snapshot (
    case_id VARCHAR(36) NOT NULL,
    version INT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, version),
    FOREIGN KEY (case_id) REFERENCES audit_case(case_id)
);

CREATE TABLE audit_request (
    request_key VARCHAR(128) PRIMARY KEY,
    fingerprint CHAR(64) NOT NULL,
    response_body TEXT NOT NULL
);
