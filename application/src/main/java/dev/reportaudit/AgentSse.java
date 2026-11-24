package dev.reportaudit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletResponse;

import java.io.IOException;

import static java.nio.charset.StandardCharsets.UTF_8;

final class AgentSse {
    private AgentSse() {}

    static void open(HttpServletResponse response) {
        response.setContentType("text/event-stream");
        response.setCharacterEncoding(UTF_8.name());
        response.setHeader("Cache-Control", "no-store");
        response.setHeader("X-Accel-Buffering", "no");
    }

    static JsonNode read(ObjectMapper json, String line) throws JsonProcessingException {
        return line.startsWith("data: ") ? json.readTree(line.substring(6)) : null;
    }

    static void emit(ObjectMapper json, HttpServletResponse response, Object event) {
        try {
            response.getOutputStream().write(("data: " + json.writeValueAsString(event) + "\n\n").getBytes(UTF_8));
            response.flushBuffer();
        } catch (IOException disconnected) {
            throw new ClientDisconnected(disconnected);
        }
    }

    static final class ClientDisconnected extends RuntimeException {
        ClientDisconnected(IOException cause) { super(cause); }
    }
}
