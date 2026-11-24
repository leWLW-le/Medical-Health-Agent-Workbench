CREATE TABLE report_reading (
    report_id VARCHAR(36) PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    title VARCHAR(180) NOT NULL,
    source_kind VARCHAR(12) NOT NULL,
    created_at BIGINT NOT NULL,
    revision INTEGER NOT NULL,
    body MEDIUMTEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES health_account(account_id) ON DELETE CASCADE
);

CREATE INDEX report_reading_account ON report_reading(account_id, created_at, report_id);
