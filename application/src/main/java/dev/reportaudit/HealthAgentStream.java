package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClientException;
import org.springframework.web.multipart.MultipartFile;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Semaphore;
import java.util.function.Consumer;

import static java.nio.charset.StandardCharsets.UTF_8;

@Component
class HealthAgentStream {
    private static final int MAX_ANSWER = 12_000;
    private static final Set<String> DISPLAY_EVENTS = Set.of("node_start", "node_end", "tool_start", "tool_end", "card");
    private static final Map<String, String> AGENT_MODES = Map.of(
        "advisor_node", "general", "clinic_node", "clinic", "pharmacy_node", "pharmacy", "report_node", "report");

    record Answer(String text, boolean awaitingReply, String thread, String agentMode) {}
    record Image(String scanType, MultipartFile file) {}

    private final ObjectMapper json;
    private final URI base;
    private final String token;
    private final Semaphore capacity;
    private static final CircuitBreakerConfig CIRCUIT_CONFIG = CircuitBreakerConfig.from(AgentClient.configuration())
        .ignoreExceptions(ApiProblem.class, AgentSse.ClientDisconnected.class).build();
    private final CircuitBreaker circuit = CircuitBreaker.of("health-agent", CIRCUIT_CONFIG);
    private final CircuitBreaker visionCircuit = CircuitBreaker.of("health-vision", CIRCUIT_CONFIG);

    HealthAgentStream(ObjectMapper json, @Value("${health.agent-url}") URI base,
                      @Value("${health.agent-token}") String token, @Value("${health.chat-concurrency}") int concurrent) {
        this.json = json;
        this.base = base;
        this.token = token;
        capacity = new Semaphore(concurrent);
    }

    void acquire() {
        if (!capacity.tryAcquire()) {
            throw new ApiProblem(HttpStatus.SERVICE_UNAVAILABLE, "HEALTH_BUSY", "健康对话请求较多，请稍后重试");
        }
    }

    void release() {
        capacity.release();
    }

    Answer run(HealthConversations.Run run, String input, String thread, boolean elderMode, boolean webSearch, Image image,
               Consumer<JsonNode> publish) {
        return (image == null ? circuit : visionCircuit)
            .executeSupplier(() -> stream(run, input, thread, elderMode, webSearch, image, publish));
    }

    private Answer stream(HealthConversations.Run run, String input, String thread, boolean elderMode, boolean webSearch, Image image,
                          Consumer<JsonNode> publish) {
        HttpURLConnection connection = null;
        var messages = new ArrayList<Map<String, String>>();
        for (var item : run.context()) {
            messages.add(Map.of("role", "user", "content", item.question()));
            if (!item.answer().isBlank()) messages.add(Map.of("role", "assistant", "content", item.answer()));
        }
        if (image == null) messages.add(Map.of("role", "user", "content", input));
        String mode = image != null ? "pharmacy" : run.agentMode() == null ? run.mode() : run.agentMode();
        long deadline = System.nanoTime() + Duration.ofSeconds(65).toNanos();
        String activeThread = thread;
        try {
            connection = connect(image == null ? "/internal/health/chat" : "/internal/health/vision-chat", "POST", 35_000);
            sendRequest(connection, messages, thread, mode, elderMode, webSearch, image);
            String contentType = connection.getContentType();
            if (connection.getResponseCode() != 200 || contentType == null || !contentType.startsWith("text/event-stream")) {
                throw new RestClientException("Unexpected health agent response");
            }
            var text = new StringBuilder();
            boolean awaiting = false;
            try (var reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), UTF_8))) {
                for (String line; (line = reader.readLine()) != null; ) {
                    if (System.nanoTime() > deadline) throw new RestClientException("Health agent deadline exceeded");
                    JsonNode event = AgentSse.read(json, line);
                    if (event == null) continue;
                    String type = event.path("type").asText();
                    switch (type) {
                        case "session" -> activeThread = checkedThread(event.path("thread_id").asText());
                        case "text" -> {
                            text.append(event.path("content").asText());
                            if (text.length() > MAX_ANSWER) throw new RestClientException("Health agent response too long");
                            publish.accept(event);
                        }
                        case "interrupt" -> {
                            awaiting = true;
                            publish.accept(event);
                        }
                        case "error" -> throw new RestClientException("Health agent reported a failure");
                        case "finish" -> {
                            return new Answer(text.toString(), awaiting, activeThread, mode);
                        }
                        default -> {
                            if ("node_start".equals(type)) mode = AGENT_MODES.getOrDefault(event.path("node").asText(), mode);
                            if (DISPLAY_EVENTS.contains(type)) publish.accept(event);
                        }
                    }
                }
            }
            throw new RestClientException("Incomplete health agent stream");
        } catch (IOException unavailable) {
            throw new RestClientException("Health agent connection failed", unavailable);
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void sendRequest(HttpURLConnection connection, ArrayList<Map<String, String>> messages,
                             String thread, String mode, boolean elderMode, boolean webSearch, Image image) throws IOException {
        connection.setDoOutput(true);
        var userInfo = Map.of("elder_mode", elderMode);
        if (image == null) {
            connection.setRequestProperty("Content-Type", "application/json");
            try (var output = connection.getOutputStream()) {
                json.writeValue(output, Map.of("messages", messages, "thread_id", thread,
                    "chat_mode", mode, "user_info", userInfo, "web_search", webSearch));
            }
            return;
        }
        String boundary = "HealthImage" + UUID.randomUUID();
        connection.setChunkedStreamingMode(16 * 1024);
        connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        try (var output = connection.getOutputStream()) {
            formField(output, boundary, "scan_type", image.scanType());
            formField(output, boundary, "thread_id", thread);
            formField(output, boundary, "user_info", json.writeValueAsString(userInfo));
            formField(output, boundary, "messages", json.writeValueAsString(messages));
            output.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"upload\"\r\n"
                + "Content-Type: " + image.file().getContentType() + "\r\n\r\n").getBytes(UTF_8));
            try (var file = image.file().getInputStream()) {
                file.transferTo(output);
            }
            output.write(("\r\n--" + boundary + "--\r\n").getBytes(UTF_8));
        }
    }

    private static void formField(OutputStream output, String boundary, String name, String value) throws IOException {
        output.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n"
            + value + "\r\n").getBytes(UTF_8));
    }

    void discard(String thread) {
        if (thread == null) return;
        HttpURLConnection connection = null;
        try {
            connection = connect("/internal/health/threads/" + checkedThread(thread), "DELETE", 1000);
            connection.getResponseCode();
        } catch (IOException unavailable) {
            // The database history is authoritative. The agent checkpoint is process-local.
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    void forwardReportPhoto(String action, String method, String account, String thread, JsonNode chat,
                            Image image, boolean elderMode, HttpServletResponse response) {
        acquire();
        HttpURLConnection connection = null;
        try {
            connection = connect("/internal/health/report-photo/" + action, method, 35_000);
            connection.setRequestProperty("X-Report-Account", account);
            if (thread != null) connection.setRequestProperty("X-Report-Thread", thread);
            if (chat != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                try (var output = connection.getOutputStream()) {
                    json.writeValue(output, chat);
                }
            } else if (image != null) {
                sendRequest(connection, new ArrayList<>(), "", "report", elderMode, false, image);
            }

            int status = connection.getResponseCode();
            response.setStatus(status);
            response.setContentType(connection.getContentType());
            response.setHeader("X-Accel-Buffering", "no");
            try (var source = status >= 400 ? connection.getErrorStream() : connection.getInputStream()) {
                if (source == null || status == 204) return;
                var target = response.getOutputStream();
                byte[] buffer = new byte[8192];
                int count;
                while ((count = source.read(buffer)) != -1) {
                    target.write(buffer, 0, count);
                    target.flush();
                }
            }
        } catch (IOException unavailable) {
            if (!response.isCommitted()) throw new RestClientException("Report photo service unavailable", unavailable);
        } finally {
            if (connection != null) connection.disconnect();
            release();
        }
    }

    private HttpURLConnection connect(String path, String method, int readTimeout) throws IOException {
        var connection = (HttpURLConnection) base.resolve(path).toURL().openConnection();
        connection.setRequestMethod(method);
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(2000);
        connection.setReadTimeout(readTimeout);
        connection.setRequestProperty("X-Agent-Token", token);
        return connection;
    }

    private static String checkedThread(String id) {
        if (!id.matches("[0-9a-f]{32}|[0-9a-f-]{36}")) throw new RestClientException("Invalid agent thread");
        return id;
    }

    void emit(HttpServletResponse response, Object event) {
        AgentSse.emit(json, response, event);
    }

}
