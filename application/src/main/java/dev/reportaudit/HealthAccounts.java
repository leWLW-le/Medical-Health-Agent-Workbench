package dev.reportaudit;

import org.springframework.dao.DuplicateKeyException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.HexFormat;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.Semaphore;

@Service
class HealthAccounts {
    static final Duration LOGIN_LIFETIME = Duration.ofHours(8);
    private static final HexFormat HEX = HexFormat.of();
    private final SecureRandom random = new SecureRandom();
    private final Semaphore passwordSlots = new Semaphore(4);
    private final JdbcTemplate database;
    private final TransactionTemplate transactions;

    record Account(String id, String username) {}
    record Login(Account account, String token) {}
    private record Credential(Account account, String salt, String hash) {}

    HealthAccounts(JdbcTemplate database, TransactionTemplate transactions) {
        this.database = database;
        this.transactions = transactions;
    }

    public Login register(String username, String password) {
        String name = username(username);
        validatePassword(password);
        Account account = new Account(UUID.randomUUID().toString(), name);
        String salt = randomHex(16);
        String hash = HEX.formatHex(passwordHash(password, salt));
        try {
            return transactions.execute(status -> {
                database.update("INSERT INTO health_account(account_id, username, salt, password_hash) VALUES (?, ?, ?, ?)",
                    account.id(), name, salt, hash);
                for (String mode : HealthConversations.MODES) {
                    database.update("INSERT INTO health_chat_session(account_id, mode, session_id) VALUES (?, ?, 'default')",
                        account.id(), mode);
                }
                return issueLogin(account);
            });
        } catch (DuplicateKeyException duplicate) {
            throw ApiProblem.conflict("USERNAME_TAKEN", "该用户名已注册");
        }
    }

    public Login login(String username, String password) {
        String name = username(username);
        validatePassword(password);
        var entries = database.query("""
            SELECT account_id, username, salt, password_hash FROM health_account WHERE username = ?
            """, (row, index) -> new Credential(new Account(row.getString(1), row.getString(2)),
            row.getString(3), row.getString(4)), name);
        Credential entry = entries.isEmpty() ? null : entries.getFirst();
        byte[] actual = passwordHash(password, entry == null ? "0".repeat(32) : entry.salt());
        if (entry == null || !MessageDigest.isEqual(actual, HEX.parseHex(entry.hash()))) {
            throw new ApiProblem(HttpStatus.UNAUTHORIZED, "INVALID_LOGIN", "用户名或密码不正确");
        }
        return issueLogin(entry.account());
    }

    Account require(String token) {
        if (token == null || !token.matches("[a-f0-9]{64}")) throw unauthorized();
        return database.query("""
            SELECT a.account_id, a.username FROM health_login l
            JOIN health_account a ON a.account_id = l.account_id
            WHERE l.token_hash = ? AND l.expires_at > ?
            """, (row, index) -> new Account(row.getString(1), row.getString(2)),
            tokenHash(token), System.currentTimeMillis()).stream().findFirst().orElseThrow(HealthAccounts::unauthorized);
    }

    void logout(String token) {
        if (token != null && token.matches("[a-f0-9]{64}")) {
            database.update("DELETE FROM health_login WHERE token_hash = ?", tokenHash(token));
        }
    }

    private Login issueLogin(Account account) {
        String token = randomHex(32);
        long now = System.currentTimeMillis();
        database.update("DELETE FROM health_login WHERE expires_at <= ?", now);
        database.update("INSERT INTO health_login(token_hash, account_id, expires_at) VALUES (?, ?, ?)",
            tokenHash(token), account.id(), now + LOGIN_LIFETIME.toMillis());
        return new Login(account, token);
    }

    private String randomHex(int length) {
        byte[] value = new byte[length];
        random.nextBytes(value);
        return HEX.formatHex(value);
    }

    private byte[] passwordHash(String password, String salt) {
        if (!passwordSlots.tryAcquire()) {
            throw new ApiProblem(HttpStatus.SERVICE_UNAVAILABLE, "AUTH_BUSY", "账号请求较多，请稍后重试");
        }
        var input = new PBEKeySpec(password.toCharArray(), HEX.parseHex(salt), 600_000, 256);
        try {
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(input).getEncoded();
        } catch (GeneralSecurityException unavailable) {
            throw new IllegalStateException("Password hashing unavailable", unavailable);
        } finally {
            input.clearPassword();
            passwordSlots.release();
        }
    }

    private static String tokenHash(String token) {
        try {
            return HEX.formatHex(MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.US_ASCII)));
        } catch (GeneralSecurityException unavailable) {
            throw new IllegalStateException("Token hashing unavailable", unavailable);
        }
    }

    private static String username(String input) {
        if (input == null || !input.matches("[a-zA-Z0-9_]{3,32}")) {
            throw ApiProblem.badInput("用户名须为 3 至 32 位字母、数字或下划线");
        }
        return input.toLowerCase(Locale.ROOT);
    }

    private static void validatePassword(String input) {
        if (input == null || input.length() < 12 || input.length() > 128) {
            throw ApiProblem.badInput("密码长度须为 12 至 128 个字符");
        }
    }

    private static ApiProblem unauthorized() {
        return new ApiProblem(HttpStatus.UNAUTHORIZED, "LOGIN_REQUIRED", "请登录后继续");
    }
}
