package dev.reportaudit;

import org.springframework.http.HttpStatus;

final class ApiProblem extends RuntimeException {
    final HttpStatus status;
    final String code;

    ApiProblem(HttpStatus status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    static ApiProblem badInput(String message) {
        return new ApiProblem(HttpStatus.UNPROCESSABLE_ENTITY, "INVALID_INPUT", message);
    }

    static ApiProblem conflict(String code, String message) {
        return new ApiProblem(HttpStatus.CONFLICT, code, message);
    }

    static ApiProblem missing(String message) {
        return new ApiProblem(HttpStatus.NOT_FOUND, "NOT_FOUND", message);
    }
}
