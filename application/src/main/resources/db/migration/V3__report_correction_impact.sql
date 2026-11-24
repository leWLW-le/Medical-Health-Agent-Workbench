CREATE TABLE audit_answer (
    answer_id VARCHAR(36) PRIMARY KEY,
    case_id VARCHAR(36) NOT NULL,
    version INT NOT NULL,
    question VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    FOREIGN KEY (case_id, version) REFERENCES audit_snapshot(case_id, version) ON DELETE CASCADE
);

CREATE INDEX audit_answer_timeline ON audit_answer(case_id, created_at, answer_id);

CREATE TABLE audit_answer_reference (
    answer_id VARCHAR(36) NOT NULL,
    field_key VARCHAR(24) NOT NULL,
    PRIMARY KEY (answer_id, field_key),
    FOREIGN KEY (answer_id) REFERENCES audit_answer(answer_id) ON DELETE CASCADE
);

CREATE TABLE audit_review (
    answer_id VARCHAR(36) NOT NULL,
    target_version INT NOT NULL,
    from_version INT NOT NULL,
    changes TEXT NOT NULL,
    reviewed_at BIGINT,
    PRIMARY KEY (answer_id, target_version),
    FOREIGN KEY (answer_id) REFERENCES audit_answer(answer_id) ON DELETE CASCADE
);
