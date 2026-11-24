package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import org.junit.jupiter.api.Test;
import org.springframework.web.client.RestClientException;

import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import static java.nio.charset.StandardCharsets.UTF_8;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class AgentClientTest {
    @Test
    void forwardsOriginalPageRequestAndReturnsBinaryImage() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        var query = new AtomicReference<String>();
        var pdf = new AtomicReference<byte[]>();
        byte[] image = {(byte) 0xff, (byte) 0xd8, (byte) 0xff, (byte) 0xd9};
        server.createContext("/internal/report-audit/scan/page", exchange -> {
            try (exchange) {
                query.set(exchange.getRequestURI().getRawQuery());
                pdf.set(exchange.getRequestBody().readAllBytes());
                exchange.getResponseHeaders().set("Content-Type", "image/jpeg");
                exchange.sendResponseHeaders(200, image.length);
                exchange.getResponseBody().write(image);
            }
        });
        server.start();
        try {
            var client = new AgentClient("http://127.0.0.1:" + server.getAddress().getPort(), "test", "http://127.0.0.1:1", "health-test");
            byte[] original = {1, 2, 3};
            assertThat(client.scanPage(original, 2, "a".repeat(64))).isEqualTo(image);
            assertThat(pdf.get()).isEqualTo(original);
            assertThat(query.get()).isEqualTo("page=2&expected_sha256=" + "a".repeat(64));
        } finally {
            server.stop(0);
        }
    }

    @Test
    void sendsReportAnswersAndStreamsToTheHealthAgent() throws Exception {
        var json = new ObjectMapper();
        var requests = new ConcurrentLinkedQueue<JsonNode>();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/internal/health/report-reading/answer", exchange -> {
            try (exchange) {
                if (!"health-test".equals(exchange.getRequestHeaders().getFirst("X-Agent-Token"))) {
                    exchange.sendResponseHeaders(401, -1);
                    return;
                }
                requests.add(json.readTree(exchange.getRequestBody()));
                boolean streaming = exchange.getRequestURI().getPath().endsWith("/stream");
                String body = streaming ? """
                    data: {"type":"text","content":"Synthetic step"}

                    data: {"type":"answer","payload":{"text":"Synthetic answer"}}

                    data: {"type":"finish"}

                    """ : "{\"text\":\"Synthetic answer\"}";
                byte[] payload = body.getBytes(UTF_8);
                exchange.getResponseHeaders().set("Content-Type", streaming ? "text/event-stream" : "application/json");
                exchange.sendResponseHeaders(200, payload.length);
                exchange.getResponseBody().write(payload);
            }
        });
        server.start();
        try {
            var client = new AgentClient("http://127.0.0.1:1", "audit-test",
                "http://127.0.0.1:" + server.getAddress().getPort(), "health-test");
            var findings = json.createArrayNode().add(json.createObjectNode().put("name", "AST"));
            var task = json.createObjectNode().put("id", "synthetic-test").put("checkpoint", "checkpoint-test");
            var replies = json.createObjectNode().put("recent_infection", "否");
            var displayEvents = new ArrayList<JsonNode>();

            var answer = client.answerReading(findings, 0, "Synthetic question", null, null, replies, "recent_infection", task);
            var streamed = client.streamReading(findings, 0, "Synthetic question", null, null, replies, "recent_infection", task, displayEvents::add);

            assertThat(answer.path("text").asText()).isEqualTo("Synthetic answer");
            assertThat(streamed).isEqualTo(answer);
            assertThat(displayEvents).extracting(event -> event.path("content").asText()).containsExactly("Synthetic step");
            assertThat(requests).hasSize(2).allSatisfy(request -> {
                assertThat(request.path("findings")).isEqualTo(findings);
                assertThat(request.path("task")).isEqualTo(task);
                assertThat(request.path("context_replies")).isEqualTo(replies);
                assertThat(request.path("reply_field").asText()).isEqualTo("recent_infection");
                assertThat(request.path("row").asInt()).isZero();
            });
        } finally {
            server.stop(0);
        }
    }

    @Test
    void repeatedUpstreamFailuresOpenTheChatCircuit() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicInteger calls = new AtomicInteger();
        server.createContext("/internal/report-audit/chat", exchange -> {
            try (exchange) {
                calls.incrementAndGet();
                exchange.getRequestBody().readAllBytes();
                exchange.sendResponseHeaders(503, -1);
            }
        });
        server.start();
        try {
            var client = new AgentClient("http://127.0.0.1:" + server.getAddress().getPort(), "test", "http://127.0.0.1:1", "health-test");
            var empty = new ObjectMapper().createObjectNode();
            for (int i = 0; i < 4; i++) {
                assertThatThrownBy(() -> client.chat(empty, empty)).isInstanceOf(RestClientException.class);
            }
            assertThatThrownBy(() -> client.chat(empty, empty)).isInstanceOf(CallNotPermittedException.class);
            assertThat(calls.get()).isEqualTo(4);
        } finally {
            server.stop(0);
        }
    }
}
