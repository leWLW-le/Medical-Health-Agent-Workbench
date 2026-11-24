# Web frontend

The React workspace provides account sign-in, text conversations, and report reading. In account mode, Vite proxies `/api/health` to the Spring Boot gateway.

```sh
npm ci
npm run dev
npm run lint
npm run build
```

See [application setup](../APPLICATION.md) for the gateway and [report testing](../REPORT_AUDIT.md) for Playwright and synthetic fixtures.
