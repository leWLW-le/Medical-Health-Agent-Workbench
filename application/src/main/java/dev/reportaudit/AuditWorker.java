package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClientException;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;

@Component
class AuditWorker {
    private static final Logger log = LoggerFactory.getLogger(AuditWorker.class);
    private final AuditRepository repository;
    private final AgentClient agent;
    private final boolean enabled;
    private final Semaphore slots;
    private final ExecutorService executor;

    AuditWorker(AuditRepository repository, AgentClient agent,
                @Value("${audit.worker-enabled}") boolean enabled,
                @Value("${audit.worker-count}") int workers) {
        this.repository = repository;
        this.agent = agent;
        this.enabled = enabled;
        slots = new Semaphore(workers);
        executor = Executors.newFixedThreadPool(workers);
    }

    @Scheduled(fixedDelayString = "${audit.worker-poll-ms}")
    void poll() {
        if (!enabled) return;
        int available = slots.availablePermits();
        for (int i = 0; i < available && slots.tryAcquire(); i++) {
            executor.execute(() -> {
                try {
                    ClaimedJob job = repository.claim();
                    if (job != null) process(job);
                } catch (RuntimeException failure) {
                    log.warn("Audit worker iteration failed ({})", failure.getClass().getSimpleName());
                } finally {
                    slots.release();
                }
            });
        }
    }

    void process(ClaimedJob job) {
        try {
            JsonNode previous = job.baseVersion() == 0 ? null :
                repository.snapshot(job.ticket().caseId(), job.baseVersion());
            JsonNode result = agent.evaluate(job, previous);
            if (!job.ticket().caseId().equals(result.path("case_id").asText())
                || job.ticket().version() != result.path("version").asInt()) {
                repository.fail(job, "AGENT_RESPONSE_MISMATCH", false);
                return;
            }
            repository.complete(job, result);
        } catch (ApiProblem problem) {
            repository.fail(job, problem.code, false);
        } catch (RestClientException | CallNotPermittedException unavailable) {
            repository.fail(job, "AGENT_UNAVAILABLE", true);
        }
    }

    @PreDestroy
    void stop() {
        executor.shutdownNow();
    }
}
