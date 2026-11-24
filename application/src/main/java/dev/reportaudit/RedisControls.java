package dev.reportaudit;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;
import java.util.function.Supplier;

@Component
class RedisControls {
    private static final DefaultRedisScript<Long> RATE_LIMIT = new DefaultRedisScript<>("""
        local count = redis.call('INCR', KEYS[1])
        if count == 1 then redis.call('EXPIRE', KEYS[1], 60) end
        return count
        """, Long.class);

    private final StringRedisTemplate redis;
    private final ObjectMapper json;
    private final boolean enabled;
    private final int limit;

    RedisControls(StringRedisTemplate redis, ObjectMapper json,
                  @Value("${audit.redis-enabled}") boolean enabled,
                  @Value("${audit.chat-per-minute}") int limit) {
        this.redis = redis;
        this.json = json;
        this.enabled = enabled;
        this.limit = limit;
    }

    void enforceChatLimit(String address) {
        enforceLimit("report-audit:chat:" + address, limit);
    }

    void enforceHealthLimit(String scope, String identity, int max) {
        enforceLimit("health-chat:" + scope + ":" + identity, max);
    }

    private void enforceLimit(String key, int max) {
        if (!enabled) return;
        Long used;
        try {
            used = redis.execute(RATE_LIMIT, List.of(key));
        } catch (DataAccessException unavailable) {
            // The AgentClient still bounds concurrent calls when Redis is unavailable.
            return;
        }
        if (used != null && used > max) {
            throw new ApiProblem(HttpStatus.TOO_MANY_REQUESTS, "CHAT_RATE_LIMIT", "问答请求过多，请稍后再试");
        }
    }

    JsonNode snapshot(String caseId, int version, Supplier<JsonNode> source) {
        if (!enabled) return source.get();
        String key = "report-audit:snapshot:" + caseId + ":" + version;
        try {
            String cached = redis.opsForValue().get(key);
            if (cached != null) return json.readTree(cached);
        } catch (DataAccessException | JsonProcessingException unavailable) {
            return source.get();
        }
        JsonNode value = source.get();
        try {
            redis.opsForValue().set(key, value.toString(), Duration.ofMinutes(5));
        } catch (DataAccessException unavailable) {
            // A cache failure does not change the authoritative result from the database.
        }
        return value;
    }
}
