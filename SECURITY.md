# Security Policy

## Credential Handling

- `legends-stripe` reads `STRIPE_SECRET_KEY` from the process environment or an explicit `--env-file <path>` argument.
- Secret keys (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`) and webhook signing secrets (`whsec_`) are never echoed in CLI output, exceptions, or generated Markdown reports.
- Outbound Stripe API requests connect exclusively to `https://api.stripe.com` with TLS certificate verification enabled.
- Webhook signature verification (`verify-webhook`) uses constant-time `hmac.compare_digest` over raw UTF-8 payload bytes and enforces a timestamp replay window (default `300` seconds).

## Reporting a Vulnerability

Please report security issues privately through GitHub Security Advisories on the repository rather than opening a public issue containing sensitive details. See [SUPPORT.md](SUPPORT.md) for general questions.
