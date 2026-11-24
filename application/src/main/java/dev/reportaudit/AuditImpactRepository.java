package dev.reportaudit;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

@Repository
class AuditImpactRepository {
    record SavedAnswer(@JsonProperty("answer_id") String id, String question, JsonNode answer) {}
    record Review(@JsonProperty("answer_id") String answerId,
                  @JsonProperty("from_version") int fromVersion,
                  @JsonProperty("target_version") int targetVersion,
                  @JsonProperty("reviewed_at") Long reviewedAt, List<ReportChanges.Change> changes) {}
    record Activity(List<SavedAnswer> answers, List<Review> reviews) {}
    private record PublishedVersion(int version, JsonNode snapshot) {}
    private static final TypeReference<List<ReportChanges.Change>> CHANGE_LIST = new TypeReference<>() {};

    private final JdbcTemplate db;
    private final ObjectMapper json;

    AuditImpactRepository(JdbcTemplate db, ObjectMapper json) {
        this.db = db;
        this.json = json;
    }

    @Transactional
    public void recordAnswer(String caseId, int version, String question, JsonNode answer) {
        // Corrections advance the same row under lock. This also handles an
        // answer generated from v1 while v2 is publishing.
        int currentVersion = db.queryForObject(
            "SELECT completed_version FROM audit_case WHERE case_id = ? FOR UPDATE", Integer.class, caseId);
        String answerId = UUID.randomUUID().toString();
        db.update("""
            INSERT INTO audit_answer(answer_id, case_id, version, question, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, answerId, caseId, version, question, answer.toString(), System.currentTimeMillis());
        Set<String> references = references(answer);
        for (String key : references) {
            db.update("INSERT INTO audit_answer_reference(answer_id, field_key) VALUES (?, ?)", answerId, key);
        }
        if (references.isEmpty() || currentVersion <= version) return;

        var versions = db.query("""
            SELECT version, body FROM audit_snapshot
            WHERE case_id = ? AND version BETWEEN ? AND ? ORDER BY version
            """, (row, index) -> new PublishedVersion(row.getInt(1), read(row.getString(2))), caseId, version, currentVersion);
        for (int i = 1; i < versions.size(); i++) {
            PublishedVersion previous = versions.get(i - 1);
            PublishedVersion current = versions.get(i);
            var impacts = ReportChanges.between(previous.snapshot(), current.snapshot()).stream()
                .filter(change -> references.contains(change.key())).toList();
            if (!impacts.isEmpty()) insertReview(answerId, previous.version(), current.version(), impacts);
        }
    }

    // Called by the audit worker within the transaction that publishes the snapshot.
    void publish(String caseId, int baseVersion, int version, JsonNode before, JsonNode after) {
        var byAnswer = new LinkedHashMap<String, List<ReportChanges.Change>>();
        for (var change : ReportChanges.between(before, after)) {
            var affected = db.queryForList("""
                SELECT a.answer_id FROM audit_answer a
                JOIN audit_answer_reference r ON r.answer_id = a.answer_id
                WHERE a.case_id = ? AND a.version < ? AND r.field_key = ?
                """, String.class, caseId, version, change.key());
            for (String answerId : affected) {
                byAnswer.computeIfAbsent(answerId, key -> new ArrayList<>()).add(change);
            }
        }
        byAnswer.forEach((answerId, changes) -> insertReview(answerId, baseVersion, version, changes));
    }

    Activity activity(String caseId) {
        return db.query("""
            SELECT a.answer_id, a.question, a.body, r.from_version, r.target_version, r.reviewed_at, r.changes
            FROM audit_answer a LEFT JOIN audit_review r ON a.answer_id = r.answer_id
            WHERE a.case_id = ?
            ORDER BY a.created_at, a.answer_id, r.target_version
            """, rows -> {
            var answers = new LinkedHashMap<String, SavedAnswer>();
            var reviews = new ArrayList<Review>();
            while (rows.next()) {
                String answerId = rows.getString(1);
                if (!answers.containsKey(answerId)) {
                    answers.put(answerId, new SavedAnswer(answerId, rows.getString(2), read(rows.getString(3))));
                }
                String changes = rows.getString(7);
                if (changes != null) {
                    reviews.add(new Review(answerId, rows.getInt(4), rows.getInt(5),
                        rows.getObject(6, Long.class), readChanges(changes)));
                }
            }
            reviews.sort(Comparator.comparingInt(Review::targetVersion).reversed());
            return new Activity(List.copyOf(answers.values()), reviews);
        }, caseId);
    }

    void markReviewed(String caseId, String answerId, int version) {
        int found = db.update("""
            UPDATE audit_review SET reviewed_at = COALESCE(reviewed_at, ?)
            WHERE answer_id = ? AND target_version = ?
              AND EXISTS (SELECT 1 FROM audit_answer a WHERE a.answer_id = ? AND a.case_id = ?)
            """, System.currentTimeMillis(), answerId, version, answerId, caseId);
        if (found == 0 && db.queryForObject("""
            SELECT COUNT(*) FROM audit_review r JOIN audit_answer a ON a.answer_id = r.answer_id
            WHERE a.case_id = ? AND r.answer_id = ? AND r.target_version = ?
            """, Integer.class, caseId, answerId, version) == 0) {
            throw ApiProblem.missing("该报告没有这项复核任务");
        }
    }

    private static Set<String> references(JsonNode answer) {
        var result = new LinkedHashSet<String>();
        for (JsonNode field : answer.path("dependencies")) {
            String key = field.asText();
            if (key.matches("summary|(HGB|WBC|PLT|GLU|ALT)\\.(presence|evidence|value|unit|collected_on|issues)")) {
                result.add(key);
            }
        }
        return result;
    }

    private void insertReview(String answerId, int baseVersion, int version, List<ReportChanges.Change> changes) {
        try {
            db.update("""
                INSERT INTO audit_review(answer_id, target_version, from_version, changes) VALUES (?, ?, ?, ?)
                """, answerId, version, baseVersion, json.writeValueAsString(changes));
        } catch (JsonProcessingException invalid) {
            throw new IllegalStateException("Cannot encode report changes", invalid);
        }
    }

    private JsonNode read(String value) {
        try {
            return json.readTree(value);
        } catch (JsonProcessingException invalid) {
            throw new IllegalStateException("Invalid stored report answer", invalid);
        }
    }

    private List<ReportChanges.Change> readChanges(String value) {
        try {
            return json.readValue(value, CHANGE_LIST);
        } catch (JsonProcessingException invalid) {
            throw new IllegalStateException("Invalid stored report changes", invalid);
        }
    }
}
