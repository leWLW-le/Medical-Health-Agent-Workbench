package dev.reportaudit;

import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseCookie;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestClientException;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;


@RestController
@RequestMapping("/api/health")
class HealthChatController {
    static final String COOKIE = "health_session";
    private static final String COOKIE_PATH = "/api/health";
    private static final String MUTATION_HEADER = "X-Health-Request=1";
    private static final Set<String> IMAGE_TYPES = Set.of("image/jpeg", "image/png", "image/webp");
    private static final Map<String, String> IMAGE_QUESTIONS = Map.of(
        "drug_box", "上传了一张药盒图片，请帮我看看这个药。",
        "trace_code", "上传了一张药品追溯码图片，请帮我查看用药信息。");
    private final HealthAccounts accounts;
    private final HealthConversations conversations;
    private final HealthAgentStream agent;
    private final RedisControls redis;
    private final boolean secure;

    record Credentials(String username, String password) {}
    record TurnRequest(UUID requestId, String text, boolean elderMode, boolean webSearch) {}

    HealthChatController(HealthAccounts accounts, HealthConversations conversations, HealthAgentStream agent,
                         RedisControls redis, @Value("${health.secure-cookie}") boolean secure) {
        this.accounts = accounts;
        this.conversations = conversations;
        this.agent = agent;
        this.redis = redis;
        this.secure = secure;
    }

    @PostMapping(value = "/register", headers = MUTATION_HEADER)
    Map<String, String> register(@RequestBody Credentials body, HttpServletRequest request, HttpServletResponse response) {
        redis.enforceHealthLimit("auth-ip", request.getRemoteAddr(), 10);
        return loginResponse(accounts.register(body.username(), body.password()), response);
    }

    @PostMapping(value = "/login", headers = MUTATION_HEADER)
    Map<String, String> login(@RequestBody Credentials body, HttpServletRequest request, HttpServletResponse response) {
        redis.enforceHealthLimit("auth-ip", request.getRemoteAddr(), 10);
        return loginResponse(accounts.login(body.username(), body.password()), response);
    }

    @GetMapping("/me")
    Map<String, String> me(@CookieValue(value = COOKIE, required = false) String token, HttpServletResponse response) {
        noStore(response);
        return Map.of("username", accounts.require(token).username());
    }

    @PostMapping(value = "/logout", headers = MUTATION_HEADER)
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void logout(@CookieValue(value = COOKIE, required = false) String token, HttpServletResponse response) {
        noStore(response);
        accounts.logout(token);
        response.addHeader(HttpHeaders.SET_COOKIE, cookie("").maxAge(0).build().toString());
    }

    @GetMapping("/chat/{mode}")
    HealthConversations.History history(@CookieValue(value = COOKIE, required = false) String token,
                                        @PathVariable String mode, @RequestParam(defaultValue = "default") String conversation,
                                        HttpServletResponse response) {
        noStore(response);
        return conversations.history(accounts.require(token).id(), mode, conversation);
    }

    @GetMapping("/conversations/{mode}")
    List<HealthConversations.Summary> list(@CookieValue(value = COOKIE, required = false) String token,
                                         @PathVariable String mode, HttpServletResponse response) {
        noStore(response);
        return conversations.list(accounts.require(token).id(), mode);
    }

    @PostMapping(value = "/conversations/{mode}", headers = MUTATION_HEADER)
    @ResponseStatus(HttpStatus.CREATED)
    Map<String, String> create(@CookieValue(value = COOKIE, required = false) String token,
                               @PathVariable String mode, HttpServletResponse response) {
        noStore(response);
        return Map.of("id", conversations.create(accounts.require(token).id(), mode));
    }

    @DeleteMapping(value = "/chat/{mode}", headers = MUTATION_HEADER)
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void clear(@CookieValue(value = COOKIE, required = false) String token, @PathVariable String mode,
               @RequestParam(defaultValue = "default") String conversation,
               HttpServletResponse response) {
        noStore(response);
        agent.discard(conversations.clear(accounts.require(token).id(), mode, conversation));
    }

    @PostMapping(value = "/chat/{mode}", headers = MUTATION_HEADER)
    void chat(@CookieValue(value = COOKIE, required = false) String token, @PathVariable String mode,
              @RequestParam(defaultValue = "default") String conversation,
              @RequestBody TurnRequest body, HttpServletResponse response) {
        noStore(response);
        String account = accounts.require(token).id();
        String question = body.text() == null ? "" : body.text().strip();
        if (body.requestId() == null || question.isEmpty() || question.length() > 2000) {
            throw ApiProblem.badInput("请输入 1 至 2000 字的文字消息");
        }
        serveTurn(account, mode, conversation, body.requestId(), question, body.elderMode(), body.webSearch(), null, response);
    }

    @PostMapping(value = "/chat/{mode}/image", headers = MUTATION_HEADER, consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    void image(@CookieValue(value = COOKIE, required = false) String token, @PathVariable String mode,
               @RequestParam(defaultValue = "default") String conversation, @RequestParam UUID requestId,
               @RequestParam String scanType, @RequestParam(defaultValue = "false") boolean elderMode,
               @RequestPart("file") MultipartFile file, HttpServletResponse response) {
        noStore(response);
        String account = accounts.require(token).id();
        String question = IMAGE_QUESTIONS.get(scanType);
        if (question == null) throw ApiProblem.badInput("请选择药盒或药品追溯码");
        if (file.getContentType() == null || !IMAGE_TYPES.contains(file.getContentType()) || file.isEmpty()) {
            throw ApiProblem.badInput("请选择有效的 JPG、PNG 或 WebP 图片");
        }
        if (file.getSize() > 8L * 1024 * 1024) {
            throw new ApiProblem(HttpStatus.PAYLOAD_TOO_LARGE, "FILE_TOO_LARGE", "请选择 8 MB 以内的图片");
        }
        serveTurn(account, mode, conversation, requestId, question, elderMode, false, new HealthAgentStream.Image(scanType, file), response);
    }

    private void serveTurn(String account, String mode, String conversation, UUID requestId, String question,
                           boolean elderMode, boolean webSearch, HealthAgentStream.Image image, HttpServletResponse response) {
        redis.enforceHealthLimit("account", account, 20);
        agent.acquire();
        try {
            handleTurn(conversations.start(account, mode, conversation, requestId, question), question, elderMode, webSearch, image, response);
        } catch (AgentSse.ClientDisconnected released) {
            // A closed browser connection cannot receive another SSE event.
        } finally {
            agent.release();
        }
    }

    private void handleTurn(HealthConversations.Run run, String question, boolean elderMode, boolean webSearch,
                            HealthAgentStream.Image image, HttpServletResponse response) {
        AgentSse.open(response);
        if (run.replay() != null) {
            emit(response, Map.of("type", "text", "content", run.replay().answer()));
            if (run.replay().awaitingReply()) emit(response, Map.of("type", "interrupt"));
            emit(response, Map.of("type", "finish"));
            return;
        }

        // Explicit image recognition can change specialists. Seed a fresh graph
        // with the saved dialogue instead of answering a pending clinic prompt.
        String upstreamThread = image != null || run.thread() == null ? UUID.randomUUID().toString() : run.thread();
        HealthAgentStream.Answer answer = null;
        boolean saved = false;
        try {
            answer = agent.run(run, question, upstreamThread, elderMode, webSearch, image, event -> emit(response, event));
            conversations.finish(run, answer.text(), answer.awaitingReply(), answer.thread(), answer.agentMode());
            saved = true;
            emit(response, Map.of("type", "finish"));
        } catch (RestClientException | CallNotPermittedException unavailable) {
            emit(response, Map.of("type", "error", "code", image == null ? "MODEL_UNAVAILABLE" : "VISION_UNAVAILABLE"));
        } finally {
            if (!saved) {
                conversations.fail(run, image != null);
                agent.discard(upstreamThread);
                if (answer != null && !answer.thread().equals(upstreamThread)) agent.discard(answer.thread());
            } else {
                if (!upstreamThread.equals(answer.thread())) agent.discard(upstreamThread);
                if (run.thread() != null && !run.thread().equals(upstreamThread) && !run.thread().equals(answer.thread())) {
                    agent.discard(run.thread());
                }
            }
        }
    }

    private void emit(HttpServletResponse response, Object event) {
        agent.emit(response, event);
    }

    private Map<String, String> loginResponse(HealthAccounts.Login login, HttpServletResponse response) {
        noStore(response);
        response.addHeader(HttpHeaders.SET_COOKIE, cookie(login.token()).maxAge(HealthAccounts.LOGIN_LIFETIME)
            .build().toString());
        return Map.of("username", login.account().username());
    }

    private ResponseCookie.ResponseCookieBuilder cookie(String token) {
        return ResponseCookie.from(COOKIE, token).path(COOKIE_PATH).httpOnly(true).sameSite("Strict").secure(secure);
    }

    private static void noStore(HttpServletResponse response) {
        response.setHeader(HttpHeaders.CACHE_CONTROL, "no-store");
    }
}
