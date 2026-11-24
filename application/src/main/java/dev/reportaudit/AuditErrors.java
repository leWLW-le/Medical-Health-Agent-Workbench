package dev.reportaudit;

import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.client.RestClientException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;

import java.util.Map;

@RestControllerAdvice
class AuditErrors {
    @ExceptionHandler(ApiProblem.class)
    ResponseEntity<Map<String, String>> application(ApiProblem problem) {
        return ResponseEntity.status(problem.status).body(Map.of("code", problem.code, "detail", problem.getMessage()));
    }

    @ExceptionHandler({RestClientException.class, CallNotPermittedException.class})
    ResponseEntity<Map<String, String>> upstream() {
        return application(new ApiProblem(HttpStatus.SERVICE_UNAVAILABLE, "AGENT_UNAVAILABLE", "智能审核服务暂时不可用"));
    }

    @ExceptionHandler(MaxUploadSizeExceededException.class)
    ResponseEntity<Map<String, String>> uploadTooLarge() {
        return application(new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "上传的文件超过大小限制"));
    }
}
