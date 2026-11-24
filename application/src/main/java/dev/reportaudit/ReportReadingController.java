package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.client.RestClientException;

import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.stream.Stream;

import static dev.reportaudit.ReportAccess.ACCOUNT;

@RestController
@RequestMapping("/api/health/report-reading")
class ReportReadingController {
    private static final int MEDICATION_DETAIL_LIMIT = 100;

    record Correction(Integer revision, JsonNode fields) {}
    record RetryOcr(Integer revision) {}
    record PatientContext(String age_group, boolean recent_infection, boolean immune_medicine,
                          boolean previous_low_lymphocytes, boolean ongoing_fever,
                          boolean unusual_bleeding, boolean severe_bleeding, String fasting,
                          boolean liver_medicine, String liver_medicine_name, String liver_medicine_since,
                          boolean kidney_medicine, boolean urinary_symptoms,
                          boolean menstrual_collection) {}
    record Question(Integer revision, Integer row, String text, PatientContext patientContext, Boolean replyToContext) {}
    private record PreparedQuestion(ObjectNode report, int revision, Integer row, String text, String reportQuestion,
                                    JsonNode context, ObjectNode contextReplies, String replyField, ObjectNode task) {}

    private final ReportReadingRepository reports;
    private final AgentClient agent;
    private final ObjectMapper json;

    ReportReadingController(ReportReadingRepository reports, AgentClient agent, ObjectMapper json) {
        this.reports = reports;
        this.agent = agent;
        this.json = json;
    }

    @GetMapping
    List<ReportReadingRepository.Summary> list(@RequestAttribute(ACCOUNT) String account,
                                               @RequestParam(defaultValue = "0") int offset) {
        if (offset < 0) throw ApiProblem.badInput("报告列表位置无效");
        return reports.list(account, offset);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    ObjectNode upload(@RequestAttribute(ACCOUNT) String account, @RequestPart("file") MultipartFile file) throws IOException {
        var extraction = (ObjectNode) agent.readReport(fileBytes(file));
        String filename = file.getOriginalFilename();
        String title = filename == null ? "" : filename.replace('\\', '/');
        title = title.substring(title.lastIndexOf('/') + 1).replaceAll("\\p{Cntrl}", "").trim();
        if (title.isEmpty()) title = "检验报告";
        return reports.create(account, title.substring(0, Math.min(title.length(), 180)), extraction);
    }

    @GetMapping("/{id}")
    ObjectNode report(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        return reports.get(account, id);
    }

    @GetMapping("/{id}/context-form")
    JsonNode contextForm(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id) {
        return agent.readingContextForm(effectiveRows(reports.get(account, id)));
    }

    @PostMapping("/{id}/retry-ocr")
    ObjectNode retryOcr(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                        @RequestPart("file") MultipartFile file, @RequestPart("retry") RetryOcr retry) throws IOException {
        var report = reports.get(account, id);
        int revision = checkRevision(report, retry.revision());
        if (!report.required("findings").isEmpty()) {
            throw ApiProblem.conflict("REPORT_ALREADY_READ", "这份报告已有检验项，可在结果表中核对字段");
        }
        var data = fileBytes(file);
        if (!sha256(data).equals(report.path("source_sha256").asText())) {
            throw ApiProblem.badInput("所选文件与报告原件不一致，请重新选择");
        }
        var extraction = agent.readReport(data);
        report.set("findings", extraction.required("findings"));
        report.set("page_count", extraction.required("page_count"));
        report.put("findings_revision", report.path("findings_revision").asInt(1) + 1);
        return reports.save(account, id, revision, report);
    }

    @PostMapping("/{id}/page")
    ResponseEntity<byte[]> page(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id,
                                @RequestPart("file") MultipartFile file, @RequestParam int page) throws IOException {
        var report = reports.get(account, id);
        if (page < 1 || page > report.path("page_count").asInt()) throw ApiProblem.badInput("原报告页码无效");
        return ResponseEntity.ok().cacheControl(CacheControl.noStore()).contentType(MediaType.IMAGE_JPEG)
            .body(agent.readingPage(fileBytes(file), page, report.path("source_sha256").asText()));
    }

    @PutMapping("/{id}/rows/{row}")
    ObjectNode correct(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id, @PathVariable int row,
                       @RequestBody Correction correction) {
        var report = reports.get(account, id);
        int revision = checkRevision(report, correction.revision());
        var original = findRow(report, row);
        var fields = correction.fields();
        if (fields == null || !fields.isObject()) throw ApiProblem.badInput("请选择要校正的检验项");
        var normalized = json.createObjectNode();
        normalized.put("name", field(fields, "name", 100));
        normalized.put("value", field(fields, "value", 100));
        normalized.put("unit", field(fields, "unit", 100));
        normalized.put("reference", field(fields, "reference", 240));
        if (normalized.path("name").asText().isBlank()) throw ApiProblem.badInput("请填写检验项目名称");
        var comparison = agent.compareReadingRow(normalized);
        var updated = original.deepCopy();
        updated.setAll(normalized);
        updated.set("status", comparison.required("status"));
        updated.set("review_reasons", comparison.required("review_reasons"));
        ((ObjectNode) report.required("corrections")).set(Integer.toString(row), updated);
        report.put("findings_revision", report.path("findings_revision").asInt(1) + 1);
        return reports.save(account, id, revision, report);
    }

    @GetMapping("/{id}/rows/{row}/explanation")
    JsonNode explain(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id, @PathVariable int row) {
        var report = reports.get(account, id);
        findRow(report, row);
        return agent.answerReading(effectiveRows(report), row, "", null, null, null, null, null);
    }

    @GetMapping(value = "/{id}/rows/{row}/explanation/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    void explainStream(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id, @PathVariable int row,
                       HttpServletResponse response) {
        var report = reports.get(account, id);
        findRow(report, row);
        stream(response, publish -> agent.streamReading(effectiveRows(report), row, "", null, null, null, null, null, publish));
    }

    @PostMapping("/{id}/questions")
    ObjectNode ask(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id, @RequestBody Question question) {
        var input = prepareQuestion(account, id, question);
        var result = agent.answerReading(effectiveRows(input.report()), input.row(), input.reportQuestion(), input.context(),
            previousTopic((ArrayNode) input.report().required("turns")), input.contextReplies(), input.replyField(), input.task());
        return saveAnswer(account, id, input, result);
    }

    @PostMapping(value = "/{id}/questions/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    void askStream(@RequestAttribute(ACCOUNT) String account, @PathVariable UUID id, @RequestBody Question question,
                   HttpServletResponse response) {
        var input = prepareQuestion(account, id, question);
        stream(response, publish -> {
            var result = agent.streamReading(effectiveRows(input.report()), input.row(), input.reportQuestion(), input.context(),
                previousTopic((ArrayNode) input.report().required("turns")), input.contextReplies(), input.replyField(), input.task(), publish);
            return saveAnswer(account, id, input, result);
        });
    }

    private PreparedQuestion prepareQuestion(String account, UUID id, Question question) {
        var report = reports.get(account, id);
        int revision = checkRevision(report, question.revision());
        var text = question.text() == null ? "" : question.text().strip();
        if (text.isEmpty() || text.length() > 400) throw ApiProblem.badInput("请输入 400 字以内的问题");
        Integer focus = question.row();
        if (focus != null) findRow(report, focus);
        var context = question.patientContext();
        if (context != null && context.age_group() != null
                && !List.of("under_18", "adult", "older_adult").contains(context.age_group())) {
            throw ApiProblem.badInput("请选择有效的年龄范围");
        }
        if (context != null && context.fasting() != null && !List.of("yes", "no", "unsure").contains(context.fasting())) {
            throw ApiProblem.badInput("请选择有效的空腹情况");
        }
        if (context != null && Stream.of(context.liver_medicine_name(), context.liver_medicine_since())
                .anyMatch(detail -> detail != null && detail.strip().length() > MEDICATION_DETAIL_LIMIT)) {
            throw ApiProblem.badInput("药物名称和开始时间各限 100 字以内");
        }
        var turns = (ArrayNode) report.required("turns");
        var replies = json.createObjectNode();
        JsonNode lastQuestion = null;
        for (var turn : turns) {
            if (turn.path("context_field").isTextual()) {
                replies.put(turn.path("context_field").asText(), turn.path("question").asText());
            } else {
                lastQuestion = turn;
            }
        }
        String reportQuestion = text;
        String replyField = null;
        int findingsRevision = report.path("findings_revision").asInt(1);
        var task = json.createObjectNode().put("id", UUID.randomUUID().toString()).put("findings_revision", findingsRevision);
        if (Boolean.TRUE.equals(question.replyToContext())) {
            var lastAnswer = turns.path(turns.size() - 1).path("answer");
            var followUp = lastAnswer.path("context_follow_up");
            if (lastQuestion == null || !followUp.path("field").isTextual()) {
                throw ApiProblem.conflict("FOLLOW_UP_CHANGED", "补问已更新，请重新打开报告后再回复。");
            }
            replyField = followUp.path("field").asText();
            if (text.length() > followUp.path("max_length").asInt(400)) {
                throw ApiProblem.badInput("这条补充超过字数限制，请简要回复。");
            }
            var previousTask = lastAnswer.path("task");
            if (previousTask.isObject()) {
                if (previousTask.path("findings_revision").asInt() != findingsRevision) {
                    throw ApiProblem.conflict("FOLLOW_UP_CHANGED", "报告字段已更新，请先按校正后的结果重新解读。");
                }
                task.put("id", previousTask.required("id").asText());
                task.put("answered", previousTask.path("answered").asInt());
                if (previousTask.path("checkpoint").isTextual()) task.set("checkpoint", previousTask.get("checkpoint"));
            }
            replies.put(replyField, text);
            reportQuestion = lastQuestion.path("question").asText();
            var questionRow = lastQuestion.path("focus_row");
            focus = questionRow.isIntegralNumber() ? questionRow.asInt() : null;
        }
        return new PreparedQuestion(report, revision, focus, text, reportQuestion,
            context == null ? null : json.valueToTree(context), replies, replyField, task);
    }

    private ObjectNode saveAnswer(String account, UUID id, PreparedQuestion input, JsonNode answer) {
        var turns = (ArrayNode) input.report().required("turns");
        var turn = json.createObjectNode().put("question", input.text());
        if (input.replyField() != null) {
            turn.put("context_field", answer.path("task").path("answered_field").asText(input.replyField()));
        }
        if (input.row() != null) turn.put("focus_row", input.row());
        turn.set("answer", answer);
        turns.add(turn);
        return reports.save(account, id, input.revision(), input.report());
    }

    private void stream(HttpServletResponse response, Function<Consumer<JsonNode>, JsonNode> run) {
        AgentSse.open(response);
        try {
            var result = run.apply(event -> AgentSse.emit(json, response, event));
            AgentSse.emit(json, response, Map.of("type", "answer", "payload", result));
            AgentSse.emit(json, response, Map.of("type", "finish"));
        } catch (ApiProblem problem) {
            AgentSse.emit(json, response, Map.of("type", "error", "code", problem.code, "content", problem.getMessage()));
        } catch (RestClientException unavailable) {
            AgentSse.emit(json, response, Map.of("type", "error", "content", "报告解读服务暂不可用，请稍后重试。"));
        } catch (AgentSse.ClientDisconnected disconnected) {
            // The report repository only stores turns after a complete agent answer.
        }
    }

    private JsonNode previousTopic(ArrayNode turns) {
        if (turns.isEmpty()) return null;
        var answer = turns.path(turns.size() - 1).path("answer");
        var topic = answer.path("topic");
        if (topic.isObject()) return topic;
        var scope = answer.path("scope").asText();
        var row = answer.path("row");
        if (!"report".equals(scope) && !"indicator".equals(scope) && !row.isIntegralNumber()) return null;
        var legacy = json.createObjectNode().put("scope", "report".equals(scope) ? "report" : "indicator");
        var selected = legacy.putArray("rows");
        if (row.isIntegralNumber()) selected.add(row.asInt());
        else if ("indicator".equals(scope)) {
            for (var citation : answer.path("citations")) {
                if (!citation.path("related").asBoolean() && citation.path("row").isIntegralNumber()) {
                    selected.add(citation.path("row").asInt());
                    if (selected.size() == 8) break;
                }
            }
        }
        return legacy;
    }

    private ArrayNode effectiveRows(ObjectNode report) {
        var result = json.createArrayNode();
        var extracted = report.required("findings");
        var corrections = report.required("corrections");
        for (int index = 0; index < extracted.size(); index++) {
            var corrected = corrections.get(Integer.toString(index));
            result.add(corrected != null ? corrected : extracted.get(index));
        }
        return result;
    }

    private static ObjectNode findRow(ObjectNode report, int row) {
        var findings = report.required("findings");
        if (row < 0 || row >= findings.size()) throw ApiProblem.missing("未找到该检验项");
        return (ObjectNode) findings.get(row);
    }

    private static int checkRevision(ObjectNode report, Integer expected) {
        int revision = report.path("revision").asInt();
        if (expected == null || expected != revision) throw ReportReadingRepository.staleReport();
        return revision;
    }

    private static String field(JsonNode fields, String name, int max) {
        var value = fields.path(name);
        if (!value.isTextual() || value.asText().length() > max) throw ApiProblem.badInput("校正字段超出允许长度或格式无效");
        return value.asText().strip();
    }

    private static byte[] fileBytes(MultipartFile file) throws IOException {
        if (file.isEmpty()) throw ApiProblem.badInput("请选择报告文件");
        if (file.getSize() > 8L * 1024 * 1024) {
            throw new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "文件大小须在 8 MB 以内");
        }
        return file.getBytes();
    }

    private static String sha256(byte[] data) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(data));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 unavailable", error);
        }
    }
}
