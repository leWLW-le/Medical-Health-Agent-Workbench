package dev.reportaudit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.support.TransactionTemplate;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Repository
class AuditRepository {
    static final int MAX_ATTEMPTS = 4;
    static final long LEASE_MS = 45_000;
    private final JdbcTemplate db;
    private final TransactionTemplate tx;
    private final ObjectMapper json;
    private final AuditImpactRepository impact;

    AuditRepository(JdbcTemplate db, TransactionTemplate tx, ObjectMapper json, AuditImpactRepository impact) {
        this.db = db;
        this.tx = tx;
        this.json = json;
        this.impact = impact;
    }

    String submit(String key, String scope, JsonNode payload, List<JsonNode> drafts, String caseId, int baseVersion) {
        return submit(null, key, scope, payload, drafts, caseId, baseVersion);
    }

    String submit(String account, String key, String scope, JsonNode payload, List<JsonNode> drafts, String caseId, int baseVersion) {
        var tickets = new ArrayList<JobTicket>();
        for (JsonNode ignored : drafts) {
            tickets.add(new JobTicket(uuid(), caseId == null ? uuid() : caseId, baseVersion + 1));
        }
        String response = write(tickets);
        String fingerprint = digest(scope + "\n" + payload.toString());
        String requestKey = key == null ? null : digest(account == null ? key : account + "\n" + key);
        try {
            return tx.execute(ignored -> {
                if (key != null) {
                    db.update("INSERT INTO audit_request VALUES (?, ?, ?)", requestKey, fingerprint, response);
                }
                for (int index = 0; index < drafts.size(); index++) {
                    insertJob(account, tickets.get(index), drafts.get(index), baseVersion);
                }
                return response;
            });
        } catch (DuplicateKeyException duplicate) {
            if (key == null) throw duplicate;
            return db.query("SELECT fingerprint, response_body FROM audit_request WHERE request_key = ?",
                rows -> {
                    if (!rows.next()) throw duplicate;
                    if (!fingerprint.equals(rows.getString(1))) {
                        throw ApiProblem.conflict("IDEMPOTENCY_CONFLICT", "同一提交键已用于其他内容");
                    }
                    return rows.getString(2);
                }, requestKey);
        }
    }

    private void insertJob(String account, JobTicket ticket, JsonNode draft, int baseVersion) {
        String patient = draft.path("patient_id").asText();
        String encounter = draft.path("encounter_id").asText();
        if (baseVersion == 0) {
            db.update("INSERT INTO audit_case (case_id, patient_id, encounter_id, submitted_version, account_id) VALUES (?, ?, ?, 1, ?)",
                ticket.caseId(), patient, encounter, account);
        } else {
            int updated = db.update("""
                UPDATE audit_case SET submitted_version = ?
                WHERE case_id = ? AND submitted_version = ? AND completed_version = ?
                    AND patient_id = ? AND encounter_id = ?
                """, ticket.version(), ticket.caseId(), baseVersion, baseVersion, patient, encounter);
            if (updated == 0) {
                ReportHead head = head(ticket.caseId());
                if (!head.patient().equals(patient) || !head.encounter().equals(encounter)) {
                    throw ApiProblem.badInput("更正版本的患者和就诊编号须与原报告相同");
                }
                throw ApiProblem.conflict("VERSION_CONFLICT", "报告已更新或正在审核，请查看当前任务");
            }
        }
        long submitted = now();
        db.update("""
            INSERT INTO audit_job (job_id, case_id, version, base_version, status, draft, available_at, created_at)
            VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?)
            """, ticket.jobId(), ticket.caseId(), ticket.version(), baseVersion, draft.toString(), submitted, submitted);
    }

    void requireCaseOwner(String caseId, String account) {
        var count = db.queryForObject("SELECT COUNT(*) FROM audit_case WHERE case_id = ? AND account_id = ?",
            Integer.class, caseId, account);
        if (count == null || count == 0) throw ApiProblem.missing("未找到该报告");
    }

    void requireJobOwner(String jobId, String account) {
        var count = db.queryForObject("""
            SELECT COUNT(*) FROM audit_job j JOIN audit_case c ON j.case_id = c.case_id
            WHERE j.job_id = ? AND c.account_id = ?
            """, Integer.class, jobId, account);
        if (count == null || count == 0) throw ApiProblem.missing("未找到该审核任务");
    }

    List<JobStatus> history(String account, int offset) {
        return db.query("""
            SELECT j.job_id, j.case_id, j.version, j.status, j.attempts, j.error_code
            FROM audit_job j JOIN audit_case c ON j.case_id = c.case_id
            WHERE c.account_id = ?
            ORDER BY j.created_at DESC, j.job_id DESC LIMIT 50 OFFSET ?
            """, (row, index) -> new JobStatus(row.getString(1), row.getString(2), row.getInt(3),
                row.getString(4), row.getInt(5), row.getString(6)), account, offset);
    }

    JsonNode workspaceDraft(String account) {
        return db.query("SELECT body FROM audit_workspace_draft WHERE account_id = ?",
            rows -> rows.next() ? read(rows.getString(1)) : json.nullNode(), account);
    }

    void saveWorkspaceDraft(String account, Map<String, ?> draft) {
        String body = write(draft);
        db.update("""
            INSERT INTO audit_workspace_draft (account_id, body) VALUES (?, ?)
            ON DUPLICATE KEY UPDATE body = ?
            """, account, body, body);
    }

    void clearWorkspaceDraft(String account) {
        db.update("DELETE FROM audit_workspace_draft WHERE account_id = ?", account);
    }

    ReportHead head(String caseId) {
        return db.query("""
            SELECT patient_id, encounter_id, submitted_version, completed_version FROM audit_case WHERE case_id = ?
            """, rows -> {
                if (!rows.next()) throw ApiProblem.missing("未找到该报告");
                return new ReportHead(rows.getString(1), rows.getString(2), rows.getInt(3), rows.getInt(4));
            }, caseId);
    }

    JobStatus status(String jobId) {
        return db.query("""
            SELECT job_id, case_id, version, status, attempts, error_code FROM audit_job WHERE job_id = ?
            """, rows -> {
                if (!rows.next()) throw ApiProblem.missing("未找到该审核任务");
                return new JobStatus(rows.getString(1), rows.getString(2), rows.getInt(3),
                    rows.getString(4), rows.getInt(5), rows.getString(6));
            }, jobId);
    }

    JobStatus currentJob(String caseId) {
        return db.query("""
            SELECT j.job_id, j.case_id, j.version, j.status, j.attempts, j.error_code
            FROM audit_case c JOIN audit_job j ON j.case_id = c.case_id AND j.version = c.submitted_version
            WHERE c.case_id = ?
            """, rows -> {
                if (!rows.next()) throw ApiProblem.missing("未找到该报告的审核任务");
                return new JobStatus(rows.getString(1), rows.getString(2), rows.getInt(3),
                    rows.getString(4), rows.getInt(5), rows.getString(6));
            }, caseId);
    }

    JsonNode snapshot(String caseId, int version) {
        return db.query("SELECT body FROM audit_snapshot WHERE case_id = ? AND version = ?", rows -> {
            if (!rows.next()) throw ApiProblem.missing("未找到已完成的报告版本");
            return read(rows.getString(1));
        }, caseId, version);
    }

    ClaimedJob claim() {
        long current = now();
        return tx.execute(ignored -> {
            List<ClaimedJob> candidate = db.query("""
                SELECT job_id, case_id, version, base_version, draft, attempts
                FROM audit_job WHERE
                    (status = 'QUEUED' AND available_at <= ?) OR (status = 'RUNNING' AND lease_until <= ?)
                ORDER BY available_at, job_id LIMIT 1 FOR UPDATE SKIP LOCKED
                """, (row, index) -> new ClaimedJob(
                    new JobTicket(row.getString(1), row.getString(2), row.getInt(3)),
                    row.getInt(4), read(row.getString(5)), row.getInt(6) + 1, uuid()),
                current, current);
            if (candidate.isEmpty()) return null;
            ClaimedJob job = candidate.getFirst();
            if (job.attempts() > MAX_ATTEMPTS) {
                db.update("""
                    UPDATE audit_job SET status = 'FAILED', error_code = 'LEASE_EXHAUSTED',
                        lease_until = NULL, claim_token = NULL WHERE job_id = ?
                    """, job.ticket().jobId());
                return null;
            }
            db.update("""
                UPDATE audit_job SET status = 'RUNNING', attempts = ?, lease_until = ?,
                    claim_token = ?, error_code = NULL WHERE job_id = ?
                """, job.attempts(), current + LEASE_MS, job.token(), job.ticket().jobId());
            return job;
        });
    }

    boolean complete(ClaimedJob job, JsonNode result) {
        return Boolean.TRUE.equals(tx.execute(ignored -> {
            if (!changeClaim(job, "SUCCEEDED", null, now())) return false;
            JobTicket ticket = job.ticket();
            db.update("UPDATE audit_case SET completed_version = ? WHERE case_id = ?", ticket.version(), ticket.caseId());
            db.update("INSERT INTO audit_snapshot VALUES (?, ?, ?)", ticket.caseId(), ticket.version(), result.toString());
            if (job.baseVersion() > 0) {
                impact.publish(ticket.caseId(), job.baseVersion(), ticket.version(),
                    snapshot(ticket.caseId(), job.baseVersion()), result);
            }
            return true;
        }));
    }

    void fail(ClaimedJob job, String code, boolean retryable) {
        boolean retry = retryable && job.attempts() < MAX_ATTEMPTS;
        long delay = 1_000L << (job.attempts() - 1);
        changeClaim(job, retry ? "QUEUED" : "FAILED", code, now() + delay);
    }

    JobStatus retry(String jobId) {
        int updated = db.update("""
            UPDATE audit_job SET status = 'QUEUED', attempts = 0, available_at = ?, error_code = NULL
            WHERE job_id = ? AND status = 'FAILED' AND error_code <> 'INVALID_INPUT'
            """, now(), jobId);
        JobStatus job = status(jobId);
        if (updated == 0 && job.status().equals("FAILED")) {
            throw ApiProblem.conflict("JOB_NOT_RETRYABLE", "此任务需要修正输入后重新提交");
        }
        return job;
    }

    private boolean changeClaim(ClaimedJob job, String status, String error, long availableAt) {
        return db.update("""
            UPDATE audit_job SET status = ?, error_code = ?, available_at = ?, lease_until = NULL, claim_token = NULL
            WHERE job_id = ? AND status = 'RUNNING' AND claim_token = ? AND lease_until > ?
            """, status, error, availableAt, job.ticket().jobId(), job.token(), now()) == 1;
    }

    private JsonNode read(String value) {
        try {
            return json.readTree(value);
        } catch (JsonProcessingException ex) {
            throw new IllegalStateException("Invalid stored JSON", ex);
        }
    }

    private String write(Object value) {
        try {
            return json.writeValueAsString(value);
        } catch (JsonProcessingException ex) {
            throw new IllegalStateException("Cannot encode audit tickets", ex);
        }
    }

    private static long now() {
        return System.currentTimeMillis();
    }

    private static String uuid() {
        return UUID.randomUUID().toString();
    }

    private static String digest(String value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException(ex);
        }
    }
}
