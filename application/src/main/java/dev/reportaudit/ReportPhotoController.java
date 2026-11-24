package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.Set;

import static dev.reportaudit.ReportAccess.ACCOUNT;

@RestController
@RequestMapping("/api/health/report-photo")
class ReportPhotoController {
    private static final Set<String> MIME_TYPES = Set.of("image/jpeg", "image/png", "image/webp");
    private final HealthAgentStream agent;

    ReportPhotoController(HealthAgentStream agent) {
        this.agent = agent;
    }

    @GetMapping("/session")
    void history(@RequestAttribute(ACCOUNT) String account, @RequestHeader("X-Report-Thread") String thread,
                 HttpServletResponse response) {
        agent.forwardReportPhoto("session", "GET", account, thread, null, null, false, response);
    }

    @DeleteMapping("/session")
    void clear(@RequestAttribute(ACCOUNT) String account, @RequestHeader("X-Report-Thread") String thread,
               HttpServletResponse response) {
        agent.forwardReportPhoto("session", "DELETE", account, thread, null, null, false, response);
    }

    @PostMapping("/chat")
    void chat(@RequestAttribute(ACCOUNT) String account, @RequestBody JsonNode body, HttpServletResponse response) {
        agent.forwardReportPhoto("chat", "POST", account, null, body, null, false, response);
    }

    @PostMapping("/vision-chat")
    void image(@RequestAttribute(ACCOUNT) String account, @RequestPart("file") MultipartFile file,
               @RequestParam(defaultValue = "false") boolean elderMode, HttpServletResponse response) {
        if (file.isEmpty() || file.getContentType() == null || !MIME_TYPES.contains(file.getContentType())) {
            throw ApiProblem.badInput("请选择有效的 JPG、PNG 或 WebP 图片");
        }
        if (file.getSize() > 8L * 1024 * 1024) {
            throw new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "请选择 8 MB 以内的图片");
        }
        agent.forwardReportPhoto("vision-chat", "POST", account, null, null, new HealthAgentStream.Image("report", file),
            elderMode, response);
    }
}
