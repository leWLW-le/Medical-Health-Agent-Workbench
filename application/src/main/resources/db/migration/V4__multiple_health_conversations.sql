CREATE TABLE health_chat_session (
    account_id VARCHAR(36) NOT NULL,
    mode VARCHAR(12) NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    agent_thread VARCHAR(64),
    agent_mode VARCHAR(12),
    lease_token VARCHAR(36),
    lease_until BIGINT,
    updated_at BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, mode, session_id),
    FOREIGN KEY (account_id) REFERENCES health_account(account_id) ON DELETE CASCADE
);

CREATE INDEX health_chat_recent ON health_chat_session(account_id, mode, updated_at);

CREATE TABLE health_chat_turn (
    sequence_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_id VARCHAR(36) NOT NULL,
    mode VARCHAR(12) NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    request_id VARCHAR(36) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    awaiting_reply BOOLEAN NOT NULL DEFAULT FALSE,
    created_at BIGINT NOT NULL,
    UNIQUE (account_id, mode, session_id, request_id),
    FOREIGN KEY (account_id, mode, session_id)
        REFERENCES health_chat_session(account_id, mode, session_id) ON DELETE CASCADE
);

CREATE INDEX health_chat_message_order ON health_chat_turn(account_id, mode, session_id, sequence_id);

INSERT INTO health_chat_session(account_id, mode, session_id, agent_thread, agent_mode, updated_at)
SELECT c.account_id, c.mode, 'default', c.agent_thread, c.agent_mode,
    COALESCE((SELECT MAX(t.created_at) FROM health_turn t WHERE t.account_id = c.account_id AND t.mode = c.mode), 0)
FROM health_conversation c;

INSERT INTO health_chat_turn(sequence_id, account_id, mode, session_id, request_id, question, answer, awaiting_reply, created_at)
SELECT sequence_id, account_id, mode, 'default', request_id, question, answer, awaiting_reply, created_at
FROM health_turn ORDER BY sequence_id;

DROP TABLE health_turn;
DROP TABLE health_conversation;
