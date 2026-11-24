ALTER TABLE audit_case ADD COLUMN account_id VARCHAR(36);
ALTER TABLE audit_case ADD CONSTRAINT audit_case_account_fk
    FOREIGN KEY (account_id) REFERENCES health_account(account_id);
CREATE INDEX audit_case_account ON audit_case(account_id);

ALTER TABLE audit_job ADD COLUMN created_at BIGINT NOT NULL DEFAULT 0;

CREATE TABLE audit_workspace_draft (
    account_id VARCHAR(36) PRIMARY KEY,
    body MEDIUMTEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES health_account(account_id) ON DELETE CASCADE
);
