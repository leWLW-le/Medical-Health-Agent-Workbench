package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.function.Consumer;

import static java.nio.charset.StandardCharsets.UTF_8;

@Component
class AgentClient {
    private static final ObjectMapper ERRORS = new ObjectMapper();
    private static final String HEALTH_READING_ANSWER = "/internal/health/report-reading/answer";
    private static final Set<String> READING_DISPLAY_EVENTS = Set.of("text", "node_start", "node_end", "tool_start", "tool_end");
    private final RestClient client;
    private final RestClient scanClient;
    private final RestClient readingModelClient;
    private final Semaphore chatSlots = new Semaphore(8);
    private final Semaphore readingSlots = new Semaphore(8);
    private final CircuitBreaker chatCircuit = CircuitBreaker.of("agent-chat", configuration());
    private final CircuitBreaker auditCircuit = CircuitBreaker.of("agent-audit", configuration());

    AgentClient(@Value("${audit.agent-url}") String url, @Value("${audit.agent-token}") String token,
                @Value("${health.agent-url}") String healthUrl, @Value("${health.agent-token}") String healthToken) {
        var transport = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(2)).build();
        var factory = new JdkClientHttpRequestFactory(transport);
        factory.setReadTimeout(Duration.ofSeconds(20));
        client = RestClient.builder().baseUrl(url).requestFactory(factory)
            .defaultHeader("X-Agent-Token", token)
            .defaultStatusHandler(status -> status.is4xxClientError(), (request, response) -> {
                if (response.getStatusCode().value() == 413) {
                    throw new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "文件大小须在 1 MB 以内");
                }
                if (response.getStatusCode().value() == 422) {
                    String path = request.getURI().getPath();
                    if (path.equals("/internal/report-audit/upload") || path.equals("/internal/report-audit/parse")
                            || path.equals("/internal/report-audit/scan") || path.equals("/internal/report-audit/scan/page")
                            || path.startsWith("/internal/report-audit/reading/")) {
                        throw reportError(response.getBody().readNBytes(4096));
                    }
                    throw ApiProblem.badInput("请检查提交的内容");
                }
                if (response.getStatusCode().value() == 429) {
                    throw new ApiProblem(HttpStatus.TOO_MANY_REQUESTS, "OCR_BUSY", "正在识别另一份报告，请稍后再试");
                }
                throw new RestClientException("Agent rejected internal request");
            }).build();
        var scanFactory = new JdkClientHttpRequestFactory(transport);
        scanFactory.setReadTimeout(Duration.ofSeconds(90));
        scanClient = client.mutate().requestFactory(scanFactory).build();
        var modelFactory = new JdkClientHttpRequestFactory(transport);
        modelFactory.setReadTimeout(Duration.ofSeconds(35));
        readingModelClient = RestClient.builder().baseUrl(healthUrl).requestFactory(modelFactory)
            .defaultHeader("X-Agent-Token", healthToken)
            .defaultStatusHandler(status -> status.value() == 422, (request, response) -> {
                throw reportError(response.getBody().readNBytes(4096));
            }).build();
    }

    private static ApiProblem reportError(byte[] body) {
        try {
            var root = ERRORS.readTree(body);
            if (root != null) {
                var detail = root.path("detail");
                if (detail.isTextual() && !detail.asText().isBlank() && detail.asText().length() <= 200) {
                    if (root.path("code").asText().equals("PDF_OCR_REQUIRED")) {
                        return new ApiProblem(HttpStatus.UNPROCESSABLE_ENTITY, "PDF_OCR_REQUIRED", detail.asText());
                    }
                    return ApiProblem.badInput(detail.asText());
                }
            }
        } catch (IOException ignored) {
            // Do not surface raw or invalid responses from the upstream service.
        }
        return ApiProblem.badInput("无法解析报告，请核对文件内容是否符合示例格式");
    }

    JsonNode demo() {
        return client.get().uri("/internal/report-audit/demo").retrieve().body(JsonNode.class);
    }

    JsonNode parseText(JsonNode payload) {
        return post("/parse", payload);
    }

    JsonNode parseFile(byte[] file) {
        return reportFile(client, "/upload", file);
    }

    JsonNode scanFile(byte[] file) {
        return reportFile(scanClient, "/scan", file);
    }

    byte[] scanPage(byte[] file, int page, String fingerprint) {
        return pageImage("/scan/page", file, page, fingerprint);
    }

    byte[] readingPage(byte[] file, int page, String fingerprint) {
        return pageImage("/reading/page", file, page, fingerprint);
    }

    private byte[] pageImage(String path, byte[] file, int page, String fingerprint) {
        byte[] image = client.post().uri(builder -> {
            builder.path("/internal/report-audit" + path).queryParam("page", page);
            if (fingerprint != null) builder.queryParam("expected_sha256", fingerprint);
            return builder.build();
        }).header("Content-Type", "application/octet-stream").body(file).retrieve().body(byte[].class);
        if (image == null) throw new RestClientException("Agent returned an empty page");
        return image;
    }

    JsonNode compareScanRow(JsonNode fields) {
        return post("/scan/compare", fields);
    }

    JsonNode readReport(byte[] file) {
        return reportFile(scanClient, "/reading/upload", file);
    }

    JsonNode compareReadingRow(JsonNode fields) {
        return post("/reading/compare", fields);
    }

    JsonNode answerReading(JsonNode findings, Integer row, String question, JsonNode patientContext, JsonNode previousTopic,
                           JsonNode contextReplies, String replyField, JsonNode task) {
        return post(readingModelClient, HEALTH_READING_ANSWER,
            readingPayload(findings, row, question, patientContext, previousTopic, contextReplies, replyField, task));
    }

    private static ObjectNode readingPayload(JsonNode findings, Integer row, String question,
                                             JsonNode patientContext, JsonNode previousTopic, JsonNode contextReplies,
                                             String replyField, JsonNode task) {
        var payload = ERRORS.createObjectNode().put("question", question);
        payload.set("findings", findings);
        if (row != null) payload.put("row", row);
        if (patientContext != null) payload.set("patient_context", patientContext);
        if (previousTopic != null) payload.set("previous_topic", previousTopic);
        if (contextReplies != null && !contextReplies.isEmpty()) payload.set("context_replies", contextReplies);
        if (replyField != null) payload.put("reply_field", replyField);
        if (task != null) payload.set("task", task);
        return payload;
    }

    JsonNode streamReading(JsonNode findings, Integer row, String question, JsonNode patientContext,
                           JsonNode previousTopic, JsonNode contextReplies, String replyField, JsonNode task, Consumer<JsonNode> publish) {
        if (!readingSlots.tryAcquire()) {
            throw new ApiProblem(HttpStatus.SERVICE_UNAVAILABLE, "AGENT_BUSY", "报告解读请求较多，请稍后重试");
        }
        try {
            return readingModelClient.post().uri(HEALTH_READING_ANSWER + "/stream")
                .contentType(MediaType.APPLICATION_JSON).accept(MediaType.TEXT_EVENT_STREAM)
                .body(readingPayload(findings, row, question, patientContext, previousTopic, contextReplies, replyField, task))
                .exchange((request, response) -> {
                    var type = response.getHeaders().getContentType();
                    if (!response.getStatusCode().is2xxSuccessful() || type == null
                            || !MediaType.TEXT_EVENT_STREAM.isCompatibleWith(type)) {
                        throw new RestClientException("Unexpected report agent stream response");
                    }
                    JsonNode answer = null;
                    try (var reader = new BufferedReader(new InputStreamReader(response.getBody(), UTF_8))) {
                        for (String line; (line = reader.readLine()) != null; ) {
                            var event = AgentSse.read(ERRORS, line);
                            if (event == null) continue;
                            String eventType = event.path("type").asText();
                            switch (eventType) {
                                case "answer" -> answer = event.path("payload");
                                case "error" -> throw new RestClientException("Report agent reported a failure");
                                case "finish" -> {
                                    if (answer == null || !answer.isObject()) throw new RestClientException("Missing final report answer");
                                    return answer;
                                }
                                default -> { if (READING_DISPLAY_EVENTS.contains(eventType)) publish.accept(event); }
                            }
                        }
                    }
                    throw new RestClientException("Incomplete report agent stream");
                });
        } finally {
            readingSlots.release();
        }
    }

    JsonNode readingContextForm(JsonNode findings) {
        return post("/reading/context-form", findings);
    }

    private static JsonNode reportFile(RestClient target, String path, byte[] file) {
        return target.post().uri("/internal/report-audit" + path)
            .header("Content-Type", "application/octet-stream").body(file).retrieve().body(JsonNode.class);
    }

    JsonNode evaluate(ClaimedJob job, JsonNode previous) {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("case_id", job.ticket().caseId());
        body.put("version", job.ticket().version());
        body.put("draft", job.draft());
        body.put("previous", previous);
        return auditCircuit.executeSupplier(() -> post("/evaluate", body));
    }

    JsonNode chat(JsonNode snapshot, JsonNode question) {
        if (!chatSlots.tryAcquire()) {
            throw new ApiProblem(HttpStatus.SERVICE_UNAVAILABLE, "AGENT_BUSY", "问答服务繁忙，请稍后重试");
        }
        try {
            return chatCircuit.executeSupplier(() -> post("/chat", Map.of("snapshot", snapshot, "question", question)));
        } finally {
            chatSlots.release();
        }
    }

    private JsonNode post(String path, Object body) {
        return post(client, "/internal/report-audit" + path, body);
    }

    private static JsonNode post(RestClient target, String path, Object body) {
        JsonNode result = target.post().uri(path)
            .header("Content-Type", "application/json").body(body).retrieve().body(JsonNode.class);
        if (result == null) throw new RestClientException("Agent returned an empty response");
        return result;
    }

    static CircuitBreakerConfig configuration() {
        return CircuitBreakerConfig.custom().slidingWindowSize(8).minimumNumberOfCalls(4)
            .failureRateThreshold(50).waitDurationInOpenState(Duration.ofSeconds(10))
            .permittedNumberOfCallsInHalfOpenState(2).ignoreExceptions(ApiProblem.class).build();
    }
}
