package dev.reportaudit;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.JsonNode;

record JobTicket(@JsonProperty("job_id") String jobId, @JsonProperty("case_id") String caseId,
                 int version) {}

record JobStatus(@JsonProperty("job_id") String jobId, @JsonProperty("case_id") String caseId,
                 int version, String status, int attempts, @JsonProperty("error_code") String errorCode) {}

record ClaimedJob(JobTicket ticket, int baseVersion, JsonNode draft, int attempts, String token) {}

record ReportHead(String patient, String encounter, int submittedVersion, int completedVersion) {}
