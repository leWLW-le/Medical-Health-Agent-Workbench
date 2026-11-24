package dev.reportaudit;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.util.List;
import java.util.Set;
import java.util.UUID;

@Repository
class HealthConversations {
    static final Set<String> MODES = Set.of("general", "clinic", "pharmacy");
    private static final long LEASE_MS = Duration.ofMinutes(2).toMillis();

    record Turn(String id, String question, String answer, boolean awaitingReply, long createdAt) {}
    record History(List<Turn> turns, boolean busy) {}
    record Summary(String id, String title, long updatedAt) {}
    record Run(String accountId, String mode, String session, String requestId, String token,
               String thread, String agentMode, List<Turn> context, Turn replay) {}
    private record Conversation(String thread, String agentMode, long busyUntil) {}

    private final JdbcTemplate database;

    HealthConversations(JdbcTemplate database) {
        this.database = database;
    }

    @Transactional(readOnly = true)
    public History history(String account, String mode, String session) {
        Conversation conversation = conversation(account, mode, session, false);
        return new History(turns(account, mode, session, 50, false), conversation.busyUntil() > System.currentTimeMillis());
    }

    List<Summary> list(String account, String mode) {
        checkMode(mode);
        return database.query("""
            SELECT s.session_id, (SELECT SUBSTRING(t.question, 1, 80) FROM health_chat_turn t
                WHERE t.account_id = s.account_id AND t.mode = s.mode AND t.session_id = s.session_id
                ORDER BY t.sequence_id LIMIT 1), s.updated_at
            FROM health_chat_session s
            WHERE s.account_id = ? AND s.mode = ? AND s.updated_at > 0
            ORDER BY s.updated_at DESC, s.session_id
            """, (row, index) -> new Summary(row.getString(1), row.getString(2), row.getLong(3)), account, mode);
    }

    String create(String account, String mode) {
        checkMode(mode);
        String session = UUID.randomUUID().toString();
        database.update("INSERT INTO health_chat_session(account_id, mode, session_id) VALUES (?, ?, ?)", account, mode, session);
        return session;
    }

    @Transactional
    public Run start(String account, String mode, String session, UUID requestId, String question) {
        Conversation conversation = conversation(account, mode, session, true);
        long now = System.currentTimeMillis();
        String id = requestId.toString();
        var previous = database.query("""
            SELECT request_id, question, answer, awaiting_reply, created_at FROM health_chat_turn
            WHERE account_id = ? AND mode = ? AND session_id = ? AND request_id = ?
            """, HealthConversations::readTurn, account, mode, session, id);
        if (!previous.isEmpty() && !previous.getFirst().question().equals(question)) {
            throw ApiProblem.conflict("REQUEST_CONFLICT", "该请求编号已用于另一条消息");
        }
        if (!previous.isEmpty() && previous.getFirst().answer() != null) {
            return new Run(account, mode, session, id, null, null, null, List.of(), previous.getFirst());
        }
        if (conversation.busyUntil() > now) throw busy();

        String token = UUID.randomUUID().toString();
        String thread = conversation.busyUntil() > 0 ? null : conversation.thread();
        database.update("""
            UPDATE health_chat_session SET lease_token = ?, lease_until = ?, updated_at = ?
            WHERE account_id = ? AND mode = ? AND session_id = ?
            """, token, now + LEASE_MS, now, account, mode, session);
        if (!previous.isEmpty()) {
            database.update("""
                DELETE FROM health_chat_turn WHERE account_id = ? AND mode = ? AND session_id = ? AND request_id = ?
                """, account, mode, session, id);
        }
        database.update("""
            INSERT INTO health_chat_turn(account_id, mode, session_id, request_id, question, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, account, mode, session, id, question, now);
        return new Run(account, mode, session, id, token, thread, conversation.agentMode(), turns(account, mode, session, 4, true), null);
    }

    @Transactional
    public void finish(Run run, String answer, boolean awaitingReply, String thread, String agentMode) {
        release(run, thread, agentMode);
        database.update("""
            UPDATE health_chat_turn SET awaiting_reply = FALSE
            WHERE account_id = ? AND mode = ? AND session_id = ? AND awaiting_reply = TRUE
            """, run.accountId(), run.mode(), run.session());
        database.update("""
            UPDATE health_chat_turn SET answer = ?, awaiting_reply = ?
            WHERE account_id = ? AND mode = ? AND session_id = ? AND request_id = ?
            """, answer, awaitingReply, run.accountId(), run.mode(), run.session(), run.requestId());
    }

    @Transactional
    public void fail(Run run, boolean preserveAgentThread) {
        database.update("""
            UPDATE health_chat_session
            SET lease_token = NULL, lease_until = NULL, agent_thread = ?
            WHERE account_id = ? AND mode = ? AND session_id = ? AND lease_token = ?
            """, preserveAgentThread ? run.thread() : null, run.accountId(), run.mode(), run.session(), run.token());
    }

    @Transactional
    public String clear(String account, String mode, String session) {
        Conversation conversation = conversation(account, mode, session, true);
        if (conversation.busyUntil() > System.currentTimeMillis()) throw busy();
        if ("default".equals(session)) {
            database.update("DELETE FROM health_chat_turn WHERE account_id = ? AND mode = ? AND session_id = 'default'", account, mode);
            database.update("""
                UPDATE health_chat_session
                SET lease_token = NULL, lease_until = NULL, agent_thread = NULL, agent_mode = NULL, updated_at = 0
                WHERE account_id = ? AND mode = ? AND session_id = 'default'
                """, account, mode);
        } else {
            database.update("DELETE FROM health_chat_session WHERE account_id = ? AND mode = ? AND session_id = ?", account, mode, session);
        }
        return conversation.thread();
    }

    private void release(Run run, String thread, String agentMode) {
        int changed = database.update("""
            UPDATE health_chat_session
            SET agent_thread = ?, agent_mode = ?, lease_token = NULL, lease_until = NULL
            WHERE account_id = ? AND mode = ? AND session_id = ? AND lease_token = ?
            """, thread, agentMode, run.accountId(), run.mode(), run.session(), run.token());
        if (changed != 1) throw busy();
    }

    private static void checkMode(String mode) {
        if (!MODES.contains(mode)) throw ApiProblem.missing("该对话模块不存在");
    }

    private Conversation conversation(String account, String mode, String session, boolean lock) {
        checkMode(mode);
        if (!"default".equals(session) && !session.matches("[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")) {
            throw ApiProblem.missing("该对话不存在");
        }
        return database.query("""
            SELECT agent_thread, agent_mode, lease_until FROM health_chat_session
            WHERE account_id = ? AND mode = ? AND session_id = ?
            """ + (lock ? " FOR UPDATE" : ""), (row, index) ->
            new Conversation(row.getString(1), row.getString(2), row.getLong(3)), account, mode, session).stream()
            .findFirst().orElseThrow(() -> ApiProblem.missing("该对话不存在"));
    }

    private List<Turn> turns(String account, String mode, String session, int limit, boolean completedOnly) {
        String completed = completedOnly ? " AND answer IS NOT NULL" : "";
        return database.query("""
            SELECT request_id, question, answer, awaiting_reply, created_at FROM health_chat_turn
            WHERE account_id = ? AND mode = ? AND session_id = ?
            """ + completed + " ORDER BY sequence_id DESC LIMIT ?", HealthConversations::readTurn, account, mode, session, limit)
            .reversed();
    }

    private static Turn readTurn(java.sql.ResultSet row, int index) throws java.sql.SQLException {
        return new Turn(row.getString(1), row.getString(2), row.getString(3), row.getBoolean(4), row.getLong(5));
    }

    private static ApiProblem busy() {
        return ApiProblem.conflict("CHAT_IN_PROGRESS", "当前对话正在生成，请稍后重试");
    }
}
