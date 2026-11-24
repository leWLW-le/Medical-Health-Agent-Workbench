package dev.reportaudit;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest(properties = {
    "spring.datasource.url=${AUDIT_TEST_JDBC_URL:jdbc:h2:mem:audit-test;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1}",
    "spring.datasource.username=${AUDIT_TEST_DB_USER:sa}", "spring.datasource.password=${AUDIT_TEST_DB_PASSWORD:}", "audit.worker-enabled=false", "audit.redis-enabled=false"
})
class AuditRepositoryTest {
    @Autowired AuditRepository repository;
    @Autowired AuditImpactRepository impact;
    @Autowired ObjectMapper json;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void resetTestTables() {
        jdbc.update("DELETE FROM audit_request");
        jdbc.update("DELETE FROM audit_snapshot");
        jdbc.update("DELETE FROM audit_job");
        jdbc.update("DELETE FROM audit_case");
    }

    @Test
    void concurrentDuplicateSubmissionsReturnOneTicket() throws Exception {
        JsonNode draft = draft("123");
        String key = UUID.randomUUID().toString();
        String accepted;
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> repository.submit(key, "case", draft, List.of(draft), null, 0));
            var second = pool.submit(() -> repository.submit(key, "case", draft, List.of(draft), null, 0));
            accepted = first.get(10, TimeUnit.SECONDS);
            assertThat(accepted).isEqualTo(second.get(10, TimeUnit.SECONDS));
        }
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM audit_request WHERE response_body = ?", Integer.class, accepted))
            .isEqualTo(1);
        JsonNode changed = draft("132");
        assertThatThrownBy(() -> repository.submit(key, "case", changed, List.of(changed), null, 0))
            .isInstanceOf(ApiProblem.class).hasMessageContaining("提交键");
    }

    @Test
    void reclaimedJobRejectsExpiredWorkerAndStoresOneResult() throws Exception {
        JobTicket ticket = create();
        ClaimedJob first = claimFor(ticket);
        jdbc.update("UPDATE audit_job SET lease_until = ? WHERE job_id = ?", 0, ticket.jobId());
        ClaimedJob second = claimFor(ticket);
        assertThat(second.attempts()).isEqualTo(2);
        assertThat(repository.complete(first, draft("123"))).isFalse();
        assertThat(repository.complete(second, draft("132"))).isTrue();
        assertThat(repository.complete(second, draft("130"))).isFalse();
        assertThat(repository.status(ticket.jobId()).status()).isEqualTo("SUCCEEDED");
        assertThat(repository.snapshot(ticket.caseId(), 1).path("rows").get(0).path("value").asText()).isEqualTo("132");
    }

    @Test
    void oneConcurrentRevisionWinsAndPreviousSnapshotRemainsReadable() throws Exception {
        JobTicket first = create();
        repository.complete(claimFor(first), draft("123"));
        JsonNode draft = draft("132");
        try (var pool = Executors.newFixedThreadPool(2)) {
            var one = pool.submit(() -> revise(first, draft));
            var two = pool.submit(() -> revise(first, draft));
            assertThat(List.of(one.get(10, TimeUnit.SECONDS), two.get(10, TimeUnit.SECONDS)))
                .containsExactlyInAnyOrder("accepted", "VERSION_CONFLICT");
        }
        ReportHead head = repository.head(first.caseId());
        assertThat(head.submittedVersion()).isEqualTo(2);
        assertThat(head.completedVersion()).isEqualTo(1);
        assertThat(repository.snapshot(first.caseId(), 1)).isEqualTo(draft("123"));
    }

    @Test
    void correctionCreatesOneReviewPerAffectedAnswerAndCatchesUpHistoricalQuestions() throws Exception {
        JobTicket first = create();
        String caseId = first.caseId();
        assertThat(repository.complete(claimFor(first), snapshot("123", true))).isTrue();
        record(caseId, 1, "HGB report", "HGB.value", "HGB.issues");
        record(caseId, 1, "WBC report", "WBC.value");
        record(caseId, 1, "HGB history", "HGB.evidence");
        record(caseId, 1, "Glossary");
        var revised = draft("132");
        String key = UUID.randomUUID().toString();
        String submitted = repository.submit(key, caseId, revised, List.of(revised), caseId, 1);
        assertThat(repository.submit(key, caseId, revised, List.of(revised), caseId, 1)).isEqualTo(submitted);
        var correction = claimFor(json.readValue(submitted, new TypeReference<List<JobTicket>>() {}).getFirst());
        assertThat(repository.complete(correction, snapshot("132", false))).isTrue();
        assertThat(repository.complete(correction, snapshot("132", false))).isFalse();
        assertThat(repository.snapshot(caseId, 1)).isEqualTo(snapshot("123", true));

        var activity = impact.activity(caseId);
        assertThat(activity.answers()).hasSize(4);
        assertThat(activity.reviews()).hasSize(1);
        var task = activity.reviews().getFirst();
        assertThat(task.answerId()).isEqualTo(activity.answers().stream().filter(a -> a.question().equals("HGB report"))
            .findFirst().orElseThrow().id());
        assertThat(task.fromVersion()).isEqualTo(1);
        assertThat(task.targetVersion()).isEqualTo(2);
        assertThat(task.changes()).extracting(ReportChanges.Change::key).containsExactly("HGB.value", "HGB.issues");
        assertThat(task.changes().getFirst().before().asText()).isEqualTo("123");
        assertThat(task.changes().getFirst().after().asText()).isEqualTo("132");
        impact.markReviewed(caseId, task.answerId(), 2);
        Long reviewedAt = impact.activity(caseId).reviews().getFirst().reviewedAt();
        impact.markReviewed(caseId, task.answerId(), 2);
        assertThat(impact.activity(caseId).reviews().getFirst().reviewedAt()).isEqualTo(reviewedAt).isNotNull();

        record(caseId, 1, "Late HGB answer", "HGB.value");
        record(caseId, 2, "Latest HGB answer", "HGB.value");
        assertThat(impact.activity(caseId).reviews()).hasSize(2);
    }

    private void record(String caseId, int version, String question, String... fields) {
        var answer = json.createObjectNode().put("version", version).put("text", question);
        var dependencies = answer.putArray("dependencies");
        for (String field : fields) dependencies.add(field);
        impact.recordAnswer(caseId, version, question, answer);
    }

    private JsonNode snapshot(String value, boolean mismatch) throws Exception {
        var fixture = json.createObjectNode();
        var hgb = fixture.putArray("findings").addObject();
        hgb.set("row", draft(value).path("rows").get(0));
        hgb.putNull("evidence");
        var issues = hgb.putArray("issues");
        if (mismatch) issues.add("value_mismatch");
        return fixture;
    }

    private String revise(JobTicket first, JsonNode draft) {
        try {
            repository.submit(UUID.randomUUID().toString(), first.caseId(), draft, List.of(draft), first.caseId(), 1);
            return "accepted";
        } catch (ApiProblem problem) {
            return problem.code;
        }
    }

    private JobTicket create() throws Exception {
        JsonNode draft = draft("123");
        String response = repository.submit(null, "case", draft, List.of(draft), null, 0);
        return json.readValue(response, new TypeReference<List<JobTicket>>() {}).getFirst();
    }

    private ClaimedJob claimFor(JobTicket ticket) {
        // Move this test's job ahead of any queued jobs left by another test.
        jdbc.update("UPDATE audit_job SET available_at = 0 WHERE job_id = ?", ticket.jobId());
        ClaimedJob job = repository.claim();
        assertThat(job).isNotNull();
        assertThat(job.ticket()).isEqualTo(ticket);
        return job;
    }

    private JsonNode draft(String value) throws Exception {
        return json.readTree("""
            {"patient_id":"DEMO-001","encounter_id":"VISIT-002",
             "rows":[{"code":"HGB","value":"%s","unit":"g/L","collected_on":"2026-08-12"}]}
            """.formatted(value));
    }
}
