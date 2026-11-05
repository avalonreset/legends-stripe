# Security Policy

## Supported Versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Credential and Network Hardening

- `legends-stripe` reads `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` from the process environment or an explicit `--env-file <path>` argument.
- Secret keys (`sk_live_`, `sk_test_`, `rk_live_`, `rk_test_`) and webhook signing secrets (`whsec_`) are automatically scrubbed and never echoed in CLI output, exception messages, or generated Markdown reports.
- Outbound Stripe API requests connect exclusively to `https://api.stripe.com` with TLS certificate verification enabled and HTTP redirects disabled (`_NoRedirectHandler`) so `Authorization` headers are never forwarded across redirects.
- Webhook probes (`webhook-probe`) and custom domain checks (`custom-domain-check`) validate that target hostnames resolve solely to public, globally routable IP addresses (blocking loopback, RFC1918 private, link-local, and cloud metadata IPs).
- Webhook signature verification (`verify-webhook`) uses constant-time `hmac.compare_digest` over raw UTF-8 payload bytes and enforces a timestamp replay window (default `300` seconds).
- Local file reads and writes reject symbolic links and enforce explicit file size bounds.

## Reporting a Vulnerability

Please report security issues privately through [GitHub Security Advisories](https://github.com/avalonreset/legends-stripe/security/advisories/new) rather than opening a public issue containing sensitive details.

- **Initial acknowledgment target:** within 48 hours.
- **Triage and remediation update target:** within 7 days.
- See [SUPPORT.md](SUPPORT.md) for general non-security usage questions.
