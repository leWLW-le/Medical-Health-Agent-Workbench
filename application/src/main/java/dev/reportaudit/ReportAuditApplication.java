package dev.reportaudit;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@EnableScheduling
@SpringBootApplication
public class ReportAuditApplication {
    public static void main(String[] args) {
        SpringApplication.run(ReportAuditApplication.class, args);
    }
}
