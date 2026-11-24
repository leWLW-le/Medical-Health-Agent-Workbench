package dev.reportaudit;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestMethodOrder;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.containsString;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest(properties = {
    "spring.datasource.url=${AUDIT_TEST_JDBC_URL:jdbc:h2:mem:health-test;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1}",
    "spring.datasource.username=${AUDIT_TEST_DB_USER:sa}", "spring.datasource.password=${AUDIT_TEST_DB_PASSWORD:}",
    "audit.worker-enabled=false", "audit.redis-enabled=false", "health.agent-token=local-test-token"
})
@AutoConfigureMockMvc
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class HealthChatTest {
    private static final AtomicInteger calls = new AtomicInteger();
    private static final AtomicInteger images = new AtomicInteger();
    private static HttpServer stub;
    private static final String PASSWORD = "synthetic-test-password";
    @Autowired MockMvc mvc;
    @Autowired ObjectMapper json;

    @DynamicPropertySource
    static void upstream(DynamicPropertyRegistry registry) throws IOException {
        stub = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        stub.createContext("/internal/health/threads", exchange -> {
            try (exchange) {
                exchange.sendResponseHeaders(204, -1);
            }
        });
        stub.createContext("/internal/report-audit/reading/upload", exchange -> {
            try (exchange) {
                exchange.getRequestBody().readAllBytes();
                String body = """
                    {"findings":[{"name":"AST","value":"17","status":"NORMAL","page":1}],"page_count":1}
                    """;
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, body.getBytes(UTF_8).length);
                exchange.getResponseBody().write(body.getBytes(UTF_8));
            }
        });
        stub.createContext("/internal/health/chat", exchange -> {
            try (exchange) {
                calls.incrementAndGet();
                var body = new ObjectMapper().readTree(exchange.getRequestBody());
                if (!"local-test-token".equals(exchange.getRequestHeaders().getFirst("X-Agent-Token"))) {
                    exchange.sendResponseHeaders(403, -1);
                    return;
                }
                if (body.path("messages").get(body.path("messages").size() - 1).path("content").asText().equals("force-failure")) {
                    exchange.sendResponseHeaders(503, -1);
                    return;
                }
                String events = """
                    data: {"type":"session","thread_id":"%s"}

                    data: {"type":"text","content":"Synthetic reply"}

                    data: {"type":"interrupt"}

                    data: {"type":"finish"}

                    """.formatted(body.path("thread_id").asText());
                byte[] payload = events.getBytes(UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
                exchange.sendResponseHeaders(200, payload.length);
                exchange.getResponseBody().write(payload);
            }
        });
        stub.createContext("/internal/health/vision-chat", exchange -> {
            try (exchange) {
                images.incrementAndGet();
                String body = new String(exchange.getRequestBody().readAllBytes(), UTF_8);
                var thread = Pattern.compile("name=\"thread_id\"\r\n\r\n([a-f0-9-]+)").matcher(body);
                if (!"local-test-token".equals(exchange.getRequestHeaders().getFirst("X-Agent-Token"))
                    || !exchange.getRequestHeaders().getFirst("Content-Type").startsWith("multipart/form-data; boundary=")
                    || !thread.find() || !body.contains("name=\"scan_type\"\r\n\r\ntrace_code")
                    || !body.contains("synthetic-image-binary") || !body.contains("Existing question")) {
                    exchange.sendResponseHeaders(422, -1);
                    return;
                }
                byte[] payload = ("""
                    data: {"type":"session","thread_id":"%s"}

                    data: {"type":"text","content":"Synthetic image recognition"}

                    data: {"type":"finish"}

                    """.formatted(thread.group(1))).getBytes(UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
                exchange.sendResponseHeaders(200, payload.length);
                exchange.getResponseBody().write(payload);
            }
        });
        stub.start();
        registry.add("health.agent-url", () -> "http://127.0.0.1:" + stub.getAddress().getPort());
        registry.add("audit.agent-url", () -> "http://127.0.0.1:" + stub.getAddress().getPort());
    }

    @AfterAll
    static void stopUpstream() {
        if (stub != null) stub.stop(0);
    }

    @Autowired ReportReadingRepository readingReports;
    @Autowired HealthAccounts healthAccounts;

    @Test
    @Order(6)
    void retriesEmptyReportOnlyWithTheOriginalFile() throws Exception {
        String user = "reader_" + UUID.randomUUID().toString().substring(0, 8);
        Cookie owner = account("/register", user);
        String accountId = healthAccounts.require(owner.getValue()).id();
        var image = "synthetic-side-by-side-report".getBytes(UTF_8);
        var fingerprint = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(image));
        var extraction = json.createObjectNode().put("source_kind", "photo").put("page_count", 1)
            .put("source_sha256", fingerprint);
        extraction.putArray("findings");
        var before = readingReports.create(accountId, "synthetic.png", extraction);
        var path = "/api/health/report-reading/" + before.path("id").asText() + "/retry-ocr";
        var revision = new MockMultipartFile("retry", "", "application/json", "{\"revision\":1}".getBytes(UTF_8));
        mvc.perform(multipart(path).file(revision)
                .file(new MockMultipartFile("file", "wrong.png", "image/png", new byte[] {1})).cookie(owner))
            .andExpect(status().isForbidden());
        mvc.perform(multipart(path).file(revision).file(new MockMultipartFile("file", "wrong.png", "image/png", new byte[] {1}))
                .header("X-Health-Request", "1").cookie(owner))
            .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").value("所选文件与报告原件不一致，请重新选择"));
        mvc.perform(multipart(path).file(revision)
                .file(new MockMultipartFile("file", "synthetic.png", "image/png", image)).header("X-Health-Request", "1").cookie(owner))
            .andExpect(status().isOk()).andExpect(jsonPath("$.id").value(before.path("id").asText()))
            .andExpect(jsonPath("$.revision").value(2)).andExpect(jsonPath("$.findings[0].name").value("AST"));
        var current = new MockMultipartFile("retry", "", "application/json", "{\"revision\":2}".getBytes(UTF_8));
        mvc.perform(multipart(path).file(current)
                .file(new MockMultipartFile("file", "synthetic.png", "image/png", image)).header("X-Health-Request", "1").cookie(owner))
            .andExpect(status().isConflict());
    }

    @Test
    @Order(1)
    void savesOnlyOwnedModuleAndReplaysDuplicateRequest() throws Exception {
        mvc.perform(get("/api/health/chat/clinic")).andExpect(status().isUnauthorized());
        String ownerName = "owner_" + UUID.randomUUID().toString().substring(0, 8);
        Cookie owner = account("/register", ownerName);
        Cookie other = account("/register", "other_" + UUID.randomUUID().toString().substring(0, 8));
        UUID id = UUID.randomUUID();
        int before = calls.get();
        send(owner, id, "synthetic symptom")
            .andExpect(content().string(containsString("\"type\":\"finish\"")));
        send(owner, id, "synthetic symptom")
            .andExpect(content().string(containsString("Synthetic reply")));
        assertThat(calls.get()).isEqualTo(before + 1);
        mvc.perform(get("/api/health/chat/clinic").cookie(owner))
            .andExpect(jsonPath("$.turns.length()").value(1))
            .andExpect(jsonPath("$.turns[0].question").value("synthetic symptom"))
            .andExpect(jsonPath("$.turns[0].answer").value("Synthetic reply"))
            .andExpect(jsonPath("$.turns[0].awaitingReply").value(true));
        mvc.perform(get("/api/health/chat/general").cookie(owner)).andExpect(jsonPath("$.turns").isEmpty());
        mvc.perform(get("/api/health/chat/clinic").cookie(other)).andExpect(jsonPath("$.turns").isEmpty());

        Cookie secondDevice = account("/login", ownerName);
        mvc.perform(get("/api/health/chat/clinic").cookie(secondDevice)).andExpect(jsonPath("$.turns.length()").value(1));
        mvc.perform(delete("/api/health/chat/clinic").cookie(owner).header("X-Health-Request", "1"))
            .andExpect(status().isNoContent());
        mvc.perform(get("/api/health/chat/clinic").cookie(secondDevice)).andExpect(jsonPath("$.turns").isEmpty());
        mvc.perform(post("/api/health/logout").cookie(owner).header("X-Health-Request", "1")).andExpect(status().isNoContent());
        mvc.perform(get("/api/health/me").cookie(owner)).andExpect(status().isUnauthorized());
    }

    @Test
    @Order(2)
    void namedConversationsCanBeOpenedIndependentlyAndDeletedByTheirOwner() throws Exception {
        Cookie owner = account("/register", "sessions_" + UUID.randomUUID().toString().substring(0, 8));
        Cookie other = account("/register", "outsider_" + UUID.randomUUID().toString().substring(0, 8));
        String path = "/api/health/chat/clinic";
        send(owner, UUID.randomUUID(), "Existing conversation");
        String body = mvc.perform(post("/api/health/conversations/clinic").cookie(owner).header("X-Health-Request", "1"))
            .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        String session = json.readTree(body).path("id").asText();
        mvc.perform(get(path).param("conversation", session).cookie(owner)).andExpect(jsonPath("$.turns").isEmpty());
        mvc.perform(post(path).param("conversation", session).cookie(owner).header("X-Health-Request", "1")
            .contentType(MediaType.APPLICATION_JSON)
            .content(json.writeValueAsString(Map.of("requestId", UUID.randomUUID(), "text", "New conversation"))))
            .andExpect(status().isOk()).andExpect(content().string(containsString("\"type\":\"finish\"")));
        mvc.perform(get("/api/health/conversations/clinic").cookie(owner)).andExpect(jsonPath("$.length()").value(2))
            .andExpect(jsonPath("$[0].title").value("New conversation"));
        mvc.perform(get(path).param("conversation", session).cookie(other)).andExpect(status().isNotFound());
        mvc.perform(delete(path).param("conversation", session).cookie(other).header("X-Health-Request", "1"))
            .andExpect(status().isNotFound());
        mvc.perform(delete(path).param("conversation", session).cookie(owner).header("X-Health-Request", "1"))
            .andExpect(status().isNoContent());
        mvc.perform(get(path).cookie(owner)).andExpect(jsonPath("$.turns[0].question").value("Existing conversation"));
        mvc.perform(get("/api/health/conversations/clinic").cookie(owner)).andExpect(jsonPath("$.length()").value(1));
    }

    @Test
    @Order(3)
    void sendsImagesThroughTheGatewayAndKeepsRepliesInTheirConversation() throws Exception {
        Cookie owner = account("/register", "image_" + UUID.randomUUID().toString().substring(0, 8));
        String path = "/api/health/chat/clinic/image";
        send(owner, UUID.randomUUID(), "Existing question");
        int before = images.get();
        var picture = new MockMultipartFile("file", "fixture.png", "image/png", "synthetic-image-binary".getBytes(UTF_8));
        mvc.perform(multipart(path).file(picture).param("scanType", "trace_code")
            .param("requestId", UUID.randomUUID().toString()).header("X-Health-Request", "1")).andExpect(status().isUnauthorized());
        mvc.perform(multipart(path).file(picture).param("scanType", "report")
            .param("requestId", UUID.randomUUID().toString()).cookie(owner).header("X-Health-Request", "1"))
            .andExpect(status().isUnprocessableEntity());
        assertThat(images.get()).isEqualTo(before);
        mvc.perform(multipart(path).file(picture).param("scanType", "trace_code")
            .param("requestId", UUID.randomUUID().toString()).cookie(owner).header("X-Health-Request", "1"))
            .andExpect(status().isOk()).andExpect(content().string(containsString("Synthetic image recognition")))
            .andExpect(content().string(containsString("\"type\":\"finish\"")));
        mvc.perform(get("/api/health/chat/clinic").cookie(owner))
            .andExpect(jsonPath("$.turns.length()").value(2))
            .andExpect(jsonPath("$.turns[1].question").value("上传了一张药品追溯码图片，请帮我查看用药信息。"))
            .andExpect(jsonPath("$.turns[1].answer").value("Synthetic image recognition"));
        mvc.perform(get("/api/health/chat/pharmacy").cookie(owner)).andExpect(jsonPath("$.turns").isEmpty());
    }

    @Test
    @Order(4)
    void keepsQuestionsAndOpensCircuitWhenUpstreamFails() throws Exception {
        Cookie owner = account("/register", "failure_" + UUID.randomUUID().toString().substring(0, 8));
        int before = calls.get();
        for (int i = 0; i < 5; i++) {
            send(owner, UUID.randomUUID(), "force-failure")
                .andExpect(content().string(containsString("MODEL_UNAVAILABLE")));
        }
        assertThat(calls.get() - before).isLessThan(5);
        mvc.perform(get("/api/health/chat/clinic").cookie(owner)).andExpect(status().isOk())
            .andExpect(jsonPath("$.turns.length()").value(5)).andExpect(jsonPath("$.busy").value(false))
            .andExpect(jsonPath("$.turns[0].answer").isEmpty());
    }

    @Test
    void reportWorkspaceRequiresLoginAndKeepsDraftsAndJobsWithTheirOwner() throws Exception {
        String reports = "/api/health/report-audit";
        mvc.perform(get(reports + "/draft")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/report-audit/jobs")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/health/report-photo/session").header("X-Report-Thread", "1".repeat(32)))
            .andExpect(status().isUnauthorized());

        String name = "reports_" + UUID.randomUUID().toString().substring(0, 8);
        Cookie owner = account("/register", name);
        Cookie secondDevice = account("/login", name);
        Cookie other = account("/register", "reports_" + UUID.randomUUID().toString().substring(0, 8));
        String draftText = """
            PATIENT DEMO-001
            ENCOUNTER VISIT-002
            HGB 123 g/L 2026-08-12
            """;
        var body = json.writeValueAsString(Map.of("text", draftText));
        mvc.perform(put(reports + "/draft").cookie(owner).contentType(MediaType.APPLICATION_JSON).content(body))
            .andExpect(status().isForbidden());
        mvc.perform(put(reports + "/draft").cookie(owner).header("X-Health-Request", "1")
                .contentType(MediaType.APPLICATION_JSON).content(body))
            .andExpect(status().isNoContent());
        mvc.perform(get(reports + "/draft").cookie(secondDevice))
            .andExpect(header().string("Cache-Control", "no-store")).andExpect(jsonPath("$.text").value(draftText));
        mvc.perform(get(reports + "/draft").cookie(other)).andExpect(content().string("null"));

        var report = """
            {"patient_id":"DEMO-001","encounter_id":"VISIT-002",
             "rows":[{"code":"HGB","value":"123","unit":"g/L","collected_on":"2026-08-12"}]}
            """;
        String key = UUID.randomUUID().toString();
        var ownTicket = submitReport(reports, owner, key, report);
        var otherTicket = submitReport(reports, other, key, report);
        String job = ownTicket.path("job_id").asText();
        String caseId = ownTicket.path("case_id").asText();
        assertThat(otherTicket.path("case_id").asText()).isNotEqualTo(caseId);
        mvc.perform(get(reports + "/jobs").cookie(secondDevice))
            .andExpect(jsonPath("$.length()").value(1)).andExpect(jsonPath("$[0].job_id").value(job));
        mvc.perform(get(reports + "/jobs/" + job).cookie(other)).andExpect(status().isNotFound());
        mvc.perform(post(reports + "/jobs/" + job + "/retry").cookie(other).header("X-Health-Request", "1"))
            .andExpect(status().isNotFound());
        mvc.perform(get(reports + "/cases/" + caseId + "?version=1").cookie(other)).andExpect(status().isNotFound());
        mvc.perform(get(reports + "/cases/" + caseId + "/activity").cookie(other)).andExpect(status().isNotFound());
        mvc.perform(delete(reports + "/draft").cookie(secondDevice).header("X-Health-Request", "1"))
            .andExpect(status().isNoContent());
        mvc.perform(get(reports + "/draft").cookie(owner)).andExpect(content().string("null"));
    }

    private com.fasterxml.jackson.databind.JsonNode submitReport(String reports, Cookie account, String key, String report)
        throws Exception {
        var response = mvc.perform(post(reports + "/cases").cookie(account).header("X-Health-Request", "1")
                .header("Idempotency-Key", key).contentType(MediaType.APPLICATION_JSON).content(report))
            .andExpect(status().isAccepted()).andReturn().getResponse();
        return json.readTree(response.getContentAsByteArray()).get(0);
    }

    private Cookie account(String path, String username) throws Exception {
        var response = mvc.perform(post("/api/health" + path).header("X-Health-Request", "1")
                .contentType(MediaType.APPLICATION_JSON).content(json.writeValueAsString(Map.of("username", username, "password", PASSWORD))))
            .andExpect(status().isOk()).andReturn().getResponse();
        String header = response.getHeader("Set-Cookie");
        assertThat(header).contains("HttpOnly", "Secure", "SameSite=Strict");
        return new Cookie("health_session", header.split(";", 2)[0].substring("health_session=".length()));
    }

    private org.springframework.test.web.servlet.ResultActions send(Cookie cookie, UUID id, String question) throws Exception {
        return mvc.perform(post("/api/health/chat/clinic").cookie(cookie).header("X-Health-Request", "1")
                .contentType(MediaType.APPLICATION_JSON).content(json.writeValueAsString(Map.of("requestId", id, "text", question))))
            .andExpect(status().isOk());
    }
}
