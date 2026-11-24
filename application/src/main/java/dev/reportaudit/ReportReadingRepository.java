package dev.reportaudit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.UUID;

@Repository
class ReportReadingRepository {
    record Summary(String id, String title, String source_kind, long created_at) {}

    private final JdbcTemplate db;
    private final ObjectMapper json;

    ReportReadingRepository(JdbcTemplate db, ObjectMapper json) {
        this.db = db;
        this.json = json;
    }

    List<Summary> list(String account, int offset) {
        return db.query("""
            SELECT report_id, title, source_kind, created_at FROM report_reading
            WHERE account_id = ? ORDER BY created_at DESC, report_id DESC LIMIT 50 OFFSET ?
            """, (row, number) -> new Summary(row.getString(1), row.getString(2), row.getString(3), row.getLong(4)),
            account, offset);
    }

    ObjectNode create(String account, String title, ObjectNode extraction) {
        var report = extraction.deepCopy();
        var id = UUID.randomUUID().toString();
        long created = System.currentTimeMillis();
        report.put("id", id).put("title", title).put("created_at", created).put("revision", 1);
        report.put("findings_revision", 1);
        report.putObject("corrections");
        report.putArray("turns");
        db.update("""
            INSERT INTO report_reading(report_id, account_id, title, source_kind, created_at, revision, body)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """, id, account, title, report.path("source_kind").asText(), created, write(report));
        return report;
    }

    ObjectNode get(String account, UUID id) {
        return db.query("SELECT body FROM report_reading WHERE account_id = ? AND report_id = ?",
            rows -> {
                if (!rows.next()) throw ApiProblem.missing("未找到该报告");
                try {
                    return (ObjectNode) json.readTree(rows.getString(1));
                } catch (JsonProcessingException error) {
                    throw new IllegalStateException("Invalid report reading", error);
                }
            }, account, id.toString());
    }

    ObjectNode save(String account, UUID id, int expected, ObjectNode report) {
        report.put("revision", expected + 1);
        int updated = db.update("""
            UPDATE report_reading SET body = ?, revision = ? WHERE account_id = ? AND report_id = ? AND revision = ?
            """, write(report), expected + 1, account, id.toString(), expected);
        if (updated != 1) throw staleReport();
        return report;
    }

    static ApiProblem staleReport() {
        return ApiProblem.conflict("REPORT_CHANGED", "该报告已更新，请重新打开报告后再提交");
    }

    private String write(ObjectNode report) {
        try {
            return json.writeValueAsString(report);
        } catch (JsonProcessingException error) {
            throw new IllegalStateException("Could not encode report reading", error);
        }
    }
}
