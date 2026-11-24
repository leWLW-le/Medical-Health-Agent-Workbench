package dev.reportaudit;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;
import org.springframework.web.util.WebUtils;

@Configuration
class ReportAccess implements WebMvcConfigurer {
    static final String ACCOUNT = "reportAccountId";
    private final HealthAccounts accounts;

    ReportAccess(HealthAccounts accounts) {
        this.accounts = accounts;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(new HandlerInterceptor() {
            @Override
            public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) {
                response.setHeader(HttpHeaders.CACHE_CONTROL, "no-store");
                var cookie = WebUtils.getCookie(request, HealthChatController.COOKIE);
                var account = accounts.require(cookie == null ? null : cookie.getValue());
                if (!"GET".equals(request.getMethod()) && !"HEAD".equals(request.getMethod())
                    && !"1".equals(request.getHeader("X-Health-Request"))) {
                    throw new ApiProblem(HttpStatus.FORBIDDEN, "REPORT_REQUEST_FORBIDDEN", "请从报告中心提交请求");
                }
                request.setAttribute(ACCOUNT, account.id());
                return true;
            }
        }).addPathPatterns("/api/report-audit/**", "/api/health/report-audit/**", "/api/health/report-photo/**",
            "/api/health/report-reading/**");
    }
}
