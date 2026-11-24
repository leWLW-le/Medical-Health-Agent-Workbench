CREATE TABLE health_account (
    account_id VARCHAR(36) PRIMARY KEY,
    username VARCHAR(32) NOT NULL UNIQUE,
    salt VARCHAR(32) NOT NULL,
    password_hash VARCHAR(64) NOT NULL
);

CREATE TABLE health_login (
    token_hash VARCHAR(64) PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    expires_at BIGINT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES health_account(account_id) ON DELETE CASCADE
);

CREATE INDEX health_login_expiry ON health_login(expires_at);

CREATE TABLE health_conversation (
    account_id VARCHAR(36) NOT NULL,
    mode VARCHAR(12) NOT NULL,
    agent_thread VARCHAR(64),
    agent_mode VARCHAR(12),
    lease_token VARCHAR(36),
    lease_until BIGINT,
    PRIMARY KEY (account_id, mode),
    FOREIGN KEY (account_id) REFERENCES health_account(account_id) ON DELETE CASCADE
);

CREATE TABLE health_turn (
    sequence_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    mode VARCHAR(12) NOT NULL,
    request_id VARCHAR(36) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    awaiting_reply BOOLEAN NOT NULL DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    UNIQUE (account_id, mode, request_id),
    FOREIGN KEY (account_id, mode) REFERENCES health_conversation(account_id, mode) ON DELETE CASCADE
);

CREATE INDEX health_turn_history ON health_turn(account_id, mode, sequence_id);
