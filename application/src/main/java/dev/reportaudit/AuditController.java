package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.CacheControl;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.io.IOException;
import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import static dev.reportaudit.ReportAccess.ACCOUNT;

@RestController
@RequestMapping({"/api/health/report-audit", "/api/report-audit"})
class AuditController {
    private static final Set<String> CODES = Set.of("HGB", "WBC", "PLT", "GLU", "ALT", "RBC", "HCT", "MCV", "AST", "CREA");
    private final AuditRepository repository;
    private final AgentClient agent;
    private final RedisControls redis;
    private final AuditImpactRepository impact;

    AuditController(AuditRepository repository, AgentClient agent, RedisControls redis, AuditImpactRepository impact) {
        this.repository = repository;
        this.agent = agent;
        this.redis = redis;
        this.impact = impact;
    }

    @GetMapping("/demo")
    JsonNode demo() {
        return agent.demo();
    }

    @PostMapping("/parse")
    JsonNode parse(@RequestBody JsonNode body) {
        return agent.parseText(body);
    }

    @PostMapping("/upload")
    JsonNode upload(@RequestAttribute(ACCOUNT) String account, @RequestPart("file") MultipartFile file,
                    @RequestParam(defaultValue = "false") boolean save) throws IOException {
        JsonNode draft = agent.parseFile(reportBytes(file));
        if (save) repository.saveWorkspaceDraft(account, Map.of("report", draft));
        return draft;
    }

    @PostMapping("/scan")
    JsonNode scan(@RequestAttribute(ACCOUNT) String account, @RequestPart("file") MultipartFile file) throws IOException {
        JsonNode preview = agent.scanFile(reportBytes(file));
        repository.saveWorkspaceDraft(account, Map.of("scan", preview));
        return preview;
    }

    @PostMapping("/scan/compare")
    JsonNode compareScanRow(@RequestBody JsonNode fields) {
        return agent.compareScanRow(fields);
    }

    @PostMapping("/scan/page")
    ResponseEntity<byte[]> scanPage(@RequestPart("file") MultipartFile file, @RequestParam int page,
                                    @RequestParam(name = "expected_sha256", required = false) String fingerprint)
            throws IOException {
        if (page < 1 || page > 5 || (fingerprint != null && !fingerprint.matches("[a-f0-9]{64}"))) {
            throw ApiProblem.badInput("原 PDF 页码或文件标识无效");
        }
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).contentType(MediaType.IMAGE_JPEG)
            .body(agent.scanPage(reportBytes(file), page, fingerprint));
    }

    @GetMapping("/draft")
    JsonNode draft(@RequestAttribute(ACCOUNT) String account) {
        return repository.workspaceDraft(account);
    }

    @PutMapping("/draft")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void saveDraft(@RequestAttribute(ACCOUNT) String account, @RequestBody JsonNode body) {
        JsonNode text = body.path("text");
        if (!text.isTextual() || text.asText().isBlank() || text.asText().length() > 10_000) {
            throw ApiProblem.badInput("请输入不超过 10,000 字的报告文本");
        }
        repository.saveWorkspaceDraft(account, Map.of("text", text.asText()));
    }

    @DeleteMapping("/draft")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void clearDraft(@RequestAttribute(ACCOUNT) String account) {
        repository.clearWorkspaceDraft(account);
    }

    @GetMapping("/jobs")
    List<JobStatus> history(@RequestAttribute(ACCOUNT) String account, @RequestParam(defaultValue = "0") int offset) {
        if (offset < 0) throw ApiProblem.badInput("历史记录偏移量无效");
        return repository.history(account, offset);
    }

    private static byte[] reportBytes(MultipartFile file) throws IOException {
        if (file.getSize() > 1024 * 1024) {
            throw new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "文件大小须在 1 MB 以内");
        }
        return file.getBytes();
    }

    @PostMapping("/cases")
    ResponseEntity<String> submit(@RequestAttribute(ACCOUNT) String account,
                                  @RequestHeader(value = "Idempotency-Key", required = false) String key,
                                  @RequestBody JsonNode draft) {
        validateDraft(draft);
        return accepted(repository.submit(account, checkKey(key), "case", draft, List.of(draft), null, 0));
    }

    @PostMapping("/batches")
    ResponseEntity<String> batch(@RequestAttribute(ACCOUNT) String account,
                                 @RequestHeader(value = "Idempotency-Key", required = false) String key,
                                 @RequestBody JsonNode body) {
        JsonNode drafts = body.path("drafts");
        if (!drafts.isArray() || drafts.isEmpty() || drafts.size() > 25) {
            throw ApiProblem.badInput("每批须包含 1 至 25 份结构化报告");
        }
        var batch = new ArrayList<JsonNode>();
        drafts.forEach(draft -> {
            validateDraft(draft);
            batch.add(draft);
        });
        return accepted(repository.submit(account, checkKey(key), "batch", body, batch, null, 0));
    }

    @PostMapping("/cases/{id}/revisions")
    ResponseEntity<String> revise(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                                  @RequestHeader(value = "Idempotency-Key", required = false) String key,
                                  @RequestBody JsonNode body) {
        repository.requireCaseOwner(id.toString(), account);
        JsonNode expected = body.path("expected_version");
        JsonNode draft = body.path("draft");
        validateDraft(draft);
        if (!expected.isIntegralNumber() || !expected.canConvertToInt()
            || expected.asInt() < 1 || expected.asInt() == Integer.MAX_VALUE) {
            throw ApiProblem.badInput("请指定有效的报告版本");
        }
        return accepted(repository.submit(account, checkKey(key), id.toString(), body, List.of(draft),
            id.toString(), expected.asInt()));
    }

    @GetMapping("/jobs/{id}")
    JobStatus job(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        repository.requireJobOwner(id.toString(), account);
        return repository.status(id.toString());
    }

    @PostMapping("/jobs/{id}/retry")
    JobStatus retry(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        repository.requireJobOwner(id.toString(), account);
        return repository.retry(id.toString());
    }

    @GetMapping("/cases/{id}/job")
    JobStatus latestJob(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        repository.requireCaseOwner(id.toString(), account);
        return repository.currentJob(id.toString());
    }

    @GetMapping("/cases/{id}")
    JsonNode snapshot(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                      @RequestParam(required = false) Integer version) {
        String caseId = id.toString();
        repository.requireCaseOwner(caseId, account);
        int resolved = version == null ? repository.head(caseId).completedVersion() : version;
        if (resolved < 1) throw ApiProblem.missing("该报告尚无已完成版本");
        return redis.snapshot(caseId, resolved, () -> repository.snapshot(caseId, resolved));
    }

    @PostMapping("/cases/{id}/chat")
    JsonNode chat(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                  @RequestBody JsonNode question, HttpServletRequest request) {
        redis.enforceChatLimit(request.getRemoteAddr());
        JsonNode version = question.path("version");
        if (!version.isIntegralNumber() || !version.canConvertToInt() || version.asInt() < 1) {
            throw ApiProblem.badInput("请选择报告版本");
        }
        JsonNode answer = agent.chat(snapshot(account, id, version.asInt()), question);
        impact.recordAnswer(id.toString(), version.asInt(), question.path("text").asText(), answer);
        return answer;
    }

    @GetMapping("/cases/{id}/activity")
    ResponseEntity<AuditImpactRepository.Activity> activity(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        String caseId = id.toString();
        repository.requireCaseOwner(caseId, account);
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).body(impact.activity(caseId));
    }

    @PostMapping("/cases/{id}/reviews/{answerId}/{version}/complete")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void completeReview(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                        @PathVariable UUID answerId, @PathVariable int version) {
        repository.requireCaseOwner(id.toString(), account);
        impact.markReviewed(id.toString(), answerId.toString(), version);
    }

    private static String checkKey(String key) {
        if (key == null) return null;
        if (!key.matches("[a-zA-Z0-9_-]{1,128}")) throw ApiProblem.badInput("提交键格式无效");
        return key;
    }

    private static ResponseEntity<String> accepted(String body) {
        return ResponseEntity.status(HttpStatus.ACCEPTED).contentType(MediaType.APPLICATION_JSON).body(body);
    }

    private static void validateDraft(JsonNode draft) {
        JsonNode rows = draft.path("rows");
        if (!draft.isObject() || draft.size() != 3 || !id(draft.path("patient_id")) || !id(draft.path("encounter_id"))
            || !rows.isArray() || rows.isEmpty() || rows.size() > CODES.size()) {
            throw ApiProblem.badInput("请核对模拟病历编号和检验项目");
        }
        Set<String> codes = new HashSet<>();
        for (JsonNode row : rows) {
            String code = row.path("code").asText();
            String unit = row.path("unit").asText();
            String date = row.path("collected_on").asText();
            JsonNode value = row.path("value");
            if (!row.isObject() || row.size() != 4 || !CODES.contains(code) || !codes.add(code)
                || !row.path("unit").isTextual() || !unit.matches("(?U)\\S{1,24}")
                || !date.matches("[0-9]{4}-[0-9]{2}-[0-9]{2}") || date.startsWith("0000-")
                || !(value.isNumber() || value.isTextual()) || value.asText().length() > 40) {
                throw ApiProblem.badInput("请核对项目代码、数值、单位和采样日期");
            }
            try {
                LocalDate.parse(date);
                BigDecimal number = new BigDecimal(value.asText()).stripTrailingZeros();
                long scale = number.scale();
                long integerDigits = Math.max(0, number.precision() - scale);
                if (scale > 6 || integerDigits > 8 || integerDigits + Math.max(0, scale) > 14) {
                    throw new NumberFormatException();
                }
            } catch (NumberFormatException | java.time.DateTimeException invalid) {
                throw ApiProblem.badInput("请核对检验数值和采样日期");
            }
        }
    }

    private static boolean id(JsonNode id) {
        return id.isTextual() && id.asText().matches("[A-Z0-9-]{1,32}");
    }
}

@Configuration
class AuditCors implements WebMvcConfigurer {
    private final String origin;

    AuditCors(@Value("${audit.cors-origin}") String origin) {
        this.origin = origin;
    }

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/report-audit/**").allowedOrigins(origin)
            .allowedMethods("GET", "POST", "PUT", "DELETE")
            .allowedHeaders("Content-Type", "Idempotency-Key", "X-Health-Request").allowCredentials(true);
    }
}
