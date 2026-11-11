"""Pure Python 3.11+ standard-library client for Stripe API v1 with form-encoding, key-mode safety, and plan hashing."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

DEFAULT_BASE_URL = "https://api.stripe.com"
ALLOWED_HOSTS = {"api.stripe.com"}
FORBIDDEN_STATEMENT_CHARS = set("<>\\'\"*")
MAX_FILE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
STRIPE_ID_RE = re.compile(r"^[a-z]{2,10}_[A-Za-z0-9_]{1,120}$")
_SECRET_SCRUB_RE = re.compile(r"\b(?:sk_live_|sk_test_|rk_live_|rk_test_|whsec_)[A-Za-z0-9_]+")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

SOFT_LAND_WEBHOOK_EVENTS = (
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


def scrub_secrets(text: str) -> str:
    """Redact any Stripe secret or webhook signing token from error or log strings."""
    return _SECRET_SCRUB_RE.sub("[REDACTED_STRIPE_SECRET]", text or "")


def validate_stripe_id(value: str, *, expected_prefix: str | tuple[str, ...] | None = None, field_name: str = "id") -> str:
    """Validate that a Stripe resource identifier is a single safe path token (prevents CWE-22 path traversal)."""
    cleaned = (value or "").strip()
    if not STRIPE_ID_RE.fullmatch(cleaned):
        raise StripeError(f"Invalid Stripe {field_name} format: {scrub_secrets(cleaned)!r}")
    if expected_prefix is not None:
        prefixes = (expected_prefix,) if isinstance(expected_prefix, str) else expected_prefix
        if not any(cleaned.startswith(p) for p in prefixes):
            raise StripeError(f"Expected Stripe {field_name} starting with {prefixes}, got {scrub_secrets(cleaned)!r}")
    return cleaned


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all automatic HTTP redirects so Authorization headers or SSRF probes never follow 3xx responses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"Redirects are disabled ({code})", headers, fp)


def build_safe_opener() -> urllib.request.OpenerDirector:
    """Build a urllib opener that refuses automatic HTTP redirects and ignores ambient proxy variables."""
    return urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.ProxyHandler({}),
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that connects TCP to a pre-validated IP while preserving TLS SNI for hostname."""

    def __init__(self, host: str, *, connect_ip: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._connect_ip, self.port or 443), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = raw_sock
            self._tunnel()
        server_host = self._tunnel_host if self._tunnel_host else self.host
        ctx = self._context if hasattr(self, "_context") and self._context is not None else ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=server_host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, connect_ip: str, context: ssl.SSLContext | None = None) -> None:
        super().__init__(context=context)
        self._connect_ip = connect_ip

    def https_open(self, req: urllib.request.Request):
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, connect_ip=self._connect_ip, **kw),
            req,
            context=self._context,
        )


def build_pinned_https_opener(connect_ip: str | None = None) -> urllib.request.OpenerDirector:
    """Build an HTTPS opener pinned to a validated IP address to prevent DNS rebinding (CWE-918)."""
    handlers: list[urllib.request.BaseHandler] = [
        _NoRedirectHandler(),
        urllib.request.ProxyHandler({}),
    ]
    if connect_ip:
        handlers.append(_PinnedHTTPSHandler(connect_ip, context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def resolve_public_https_host(hostname: str, *, resolve_dns: bool = True) -> tuple[str, list[str]]:
    """Reject loopback, link-local, RFC1918, CGNAT, multicast, and internal hostnames/IPs, failing closed on DNS errors."""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host or "/" in host or "@" in host or ":" in host or " " in host:
        raise StripeError(f"Invalid target hostname: {hostname!r}")
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")):
        raise StripeError(f"Refusing non-public or local hostname: {host}")

    def _check_ip(ip_str: str) -> None:
        addr = ipaddress.ip_address(ip_str)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NET)
            or not addr.is_global
        ):
            raise StripeError(f"Refusing non-public IP address target: {ip_str}")

    try:
        _check_ip(host)
        return host, [host]
    except ValueError:
        pass

    resolved_ips: list[str] = []
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise StripeError(f"DNS resolution failed for target host '{host}': {exc}") from exc
        if not infos:
            raise StripeError(f"DNS resolution returned no addresses for target host '{host}'")
        for family, _, _, _, sockaddr in infos:
            if family in (socket.AF_INET, socket.AF_INET6) and sockaddr:
                ip_val = str(sockaddr[0])
                _check_ip(ip_val)
                if ip_val not in resolved_ips:
                    resolved_ips.append(ip_val)
    return host, resolved_ips


def validate_public_https_host(hostname: str, *, resolve_dns: bool = True) -> str:
    """Reject loopback, link-local, RFC1918 private, CGNAT, multicast, and internal hostnames/IPs (CWE-918 SSRF defense)."""
    host, _ = resolve_public_https_host(hostname, resolve_dns=resolve_dns)
    return host


_MACOS_SYSTEM_SYMLINKS = {
    Path("/var"),
    Path("/tmp"),
    Path("/etc"),
    Path("/private/var"),
    Path("/private/tmp"),
    Path("/private/etc"),
}


def _reject_symlink_chain(path: Path) -> None:
    """Refuse symlinks on the target path or any non-system parent directory (CWE-59)."""
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise StripeError(f"Refusing to operate on symlink path: {raw}")
    for parent in raw.absolute().parents:
        if sys.platform == "darwin" and parent in _MACOS_SYSTEM_SYMLINKS:
            continue
        if parent.is_symlink():
            raise StripeError(f"Refusing to traverse symlinked directory: {parent}")


def safe_read_text_file(path: str | Path, *, max_bytes: int = MAX_FILE_BYTES, label: str = "file") -> tuple[Path, str]:
    """Read a regular UTF-8 file after verifying it is not a symlink and does not exceed max_bytes."""
    raw_path = Path(path).expanduser()
    _reject_symlink_chain(raw_path)
    target = raw_path.resolve()
    if not target.is_file() or target.is_symlink():
        raise StripeError(f"Requested {label} does not exist or is not a regular file: {target}")
    size = target.stat().st_size
    if size > max_bytes:
        raise StripeError(f"Requested {label} exceeds {max_bytes} bytes ({size} bytes): {target}")
    return target, target.read_text(encoding="utf-8", errors="replace")


def safe_write_text_file(path_str: str | Path, content: str, *, overwrite: bool = False) -> str:
    """Write a single UTF-8 report file safely without following symlinks or silently overwriting existing files."""
    raw_path = Path(path_str).expanduser()
    _reject_symlink_chain(raw_path)
    target = raw_path.resolve()
    if target.exists() and target.is_symlink():
        raise StripeError(f"Refusing to write to symlink path: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        with target.open("w", encoding="utf-8") as handle:
            handle.write(content)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(target), flags, 0o600)
        except FileExistsError:
            raise StripeError(f"Output file already exists (pass --overwrite to replace): {target}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    return str(target)


class StripeError(RuntimeError):
    """Structured error from Stripe API or local safety validation."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 param: str | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.param = param
        self.request_id = request_id

    @property
    def stripe_code(self) -> str | None:
        return self.code

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": str(self)}
        if self.status is not None:
            out["status"] = self.status
        if self.code:
            out["code"] = self.code
        if self.param:
            out["param"] = self.param
        if self.request_id:
            out["request_id"] = self.request_id
        return out


def classify_key(key: str | None) -> dict[str, Any]:
    """Classify a Stripe key by prefix without exposing the secret value."""
    raw = (key or "").strip()
    if not raw:
        return {"present": False, "kind": "missing", "mode": "none", "valid_secret": False, "prefix": None}
    for prefix, kind, mode, valid_secret in (
        ("sk_test_", "secret", "test", True),
        ("rk_test_", "restricted", "test", True),
        ("sk_live_", "secret", "live", True),
        ("rk_live_", "restricted", "live", True),
        ("pk_test_", "publishable", "test", False),
        ("pk_live_", "publishable", "live", False),
        ("whsec_", "webhook_secret", "unknown", False),
    ):
        if raw.startswith(prefix):
            return {
                "present": True,
                "kind": kind,
                "mode": mode,
                "valid_secret": valid_secret,
                "prefix": prefix.rstrip("_"),
            }
    return {"present": True, "kind": "unknown", "mode": "unknown", "valid_secret": False, "prefix": "unknown"}


classify_stripe_key = classify_key


def load_dotenv_file(path: str | Path) -> dict[str, str]:
    """Read an explicit .env file requested by the operator without persisting state."""
    _, content = safe_read_text_file(path, label="env file")
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        values[k] = v
    return values


def resolve_credentials(env_file: str | Path | None = None) -> dict[str, Any]:
    """Resolve Stripe configuration from environment variables or an explicit --env-file."""
    source = dict(os.environ)
    if env_file:
        file_vars = load_dotenv_file(env_file)
        for k, v in file_vars.items():
            if k not in source or not source[k]:
                source[k] = v
    secret_key = (source.get("STRIPE_SECRET_KEY") or source.get("STRIPE_API_KEY") or "").strip()
    publishable_key = (source.get("STRIPE_PUBLISHABLE_KEY") or "").strip()
    webhook_secret = (source.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    api_version = (source.get("STRIPE_API_VERSION") or "").strip()
    stripe_account = (source.get("STRIPE_ACCOUNT") or "").strip()
    base_url = (source.get("STRIPE_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    info = classify_key(secret_key)
    return {
        "secret_key": secret_key,
        "publishable_key": publishable_key,
        "webhook_secret": webhook_secret,
        "api_version": api_version or None,
        "stripe_account": stripe_account or None,
        "base_url": base_url,
        "key_info": info,
        "publishable_present": bool(publishable_key),
        "webhook_secret_present": bool(webhook_secret and webhook_secret.startswith("whsec_")),
    }


def flatten_stripe_params(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten nested dicts and lists into Stripe's bracketed x-www-form-urlencoded pairs."""
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key in data:
            val = data[key]
            if val is None:
                continue
            next_prefix = f"{prefix}[{key}]" if prefix else str(key)
            pairs.extend(flatten_stripe_params(val, next_prefix))
    elif isinstance(data, (list, tuple)):
        for idx, item in enumerate(data):
            if item is None:
                continue
            next_prefix = f"{prefix}[{idx}]"
            pairs.extend(flatten_stripe_params(item, next_prefix))
    elif isinstance(data, bool):
        pairs.append((prefix, "true" if data else "false"))
    else:
        pairs.append((prefix, str(data)))
    return pairs


def encode_stripe_form(data: dict[str, Any] | None) -> bytes:
    """Encode a parameter dictionary into UTF-8 application/x-www-form-urlencoded bytes."""
    if not data:
        return b""
    pairs = flatten_stripe_params(data)
    return urllib.parse.urlencode(pairs, doseq=True).encode("utf-8")


def plan_digest(action: str, payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 digest for a previewed mutation plan."""
    canonical = json.dumps({"action": action, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_statement_descriptor(value: str | None, *, field_name: str = "statement_descriptor") -> list[str]:
    """Validate Stripe's 22-character ASCII statement descriptor rules."""
    issues: list[str] = []
    if value is None or value == "":
        return issues
    if len(value) > 22:
        issues.append(f"{field_name} exceeds 22 characters ({len(value)} chars)")
    if len(value) < 5:
        issues.append(f"{field_name} must be at least 5 characters ({len(value)} chars)")
    if not value.isascii():
        issues.append(f"{field_name} must contain only printable ASCII characters")
    bad = sorted(ch for ch in set(value) if ch in FORBIDDEN_STATEMENT_CHARS)
    if bad:
        issues.append(f"{field_name} contains forbidden characters: {''.join(bad)}")
    if not any(ch.isalpha() for ch in value):
        issues.append(f"{field_name} must contain at least one letter")
    return issues


def validate_metadata(metadata: dict[str, Any] | None) -> list[str]:
    """Validate Stripe metadata limits (max 50 keys, 40-char keys, 500-char values)."""
    issues: list[str] = []
    if not metadata:
        return issues
    if len(metadata) > 50:
        issues.append(f"metadata has {len(metadata)} keys (maximum 50)")
    for k, v in metadata.items():
        ks = str(k)
        vs = "" if v is None else str(v)
        if len(ks) > 40:
            issues.append(f"metadata key '{ks[:20]}...' exceeds 40 characters ({len(ks)})")
        if len(vs) > 500:
            issues.append(f"metadata value for '{ks}' exceeds 500 characters ({len(vs)})")
    return issues


def validate_product_fields(
    *,
    name: str,
    description: str | None = None,
    statement_descriptor: str | None = None,
    marketing_features: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Validate product creation/update fields against Stripe operational limits."""
    issues: list[str] = []
    if not name or not name.strip():
        issues.append("product name must not be empty")
    elif len(name) > 250:
        issues.append(f"product name exceeds 250 characters ({len(name)})")
    if description is not None and len(description) > 500:
        issues.append(f"product description exceeds 500-character invoice/checkout limit ({len(description)} chars)")
    issues.extend(validate_statement_descriptor(statement_descriptor))
    if marketing_features:
        if len(marketing_features) > 15:
            issues.append(f"marketing_features has {len(marketing_features)} items (maximum 15)")
        for idx, feat in enumerate(marketing_features):
            if len(feat) > 80:
                issues.append(f"marketing_features[{idx}] exceeds 80 characters ({len(feat)})")
    issues.extend(validate_metadata(metadata))
    return issues


def validate_price_fields(
    *,
    currency: str,
    unit_amount: int,
    interval: str | None = None,
    interval_count: int = 1,
    nickname: str | None = None,
    lookup_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Validate price creation fields against Stripe rules."""
    issues: list[str] = []
    if not re.fullmatch(r"[a-zA-Z]{3}", currency or ""):
        issues.append("currency must be a 3-letter ISO code (for example 'usd')")
    if not isinstance(unit_amount, int) or unit_amount < 0:
        issues.append("unit_amount must be a non-negative integer in minor units (cents)")
    if unit_amount > 99999999:
        issues.append("unit_amount exceeds Stripe maximum (99999999)")
    if interval is not None:
        if interval not in ("day", "week", "month", "year"):
            issues.append("interval must be one of: day, week, month, year")
        if interval_count < 1:
            issues.append("interval_count must be >= 1")
        max_intervals = {"day": 365 * 3, "week": 52 * 3, "month": 36, "year": 3}
        if interval in max_intervals and interval_count > max_intervals[interval]:
            issues.append(f"interval_count={interval_count} for interval='{interval}' exceeds Stripe 3-year billing maximum")
    if nickname is not None and len(nickname) > 250:
        issues.append(f"price nickname exceeds 250 characters ({len(nickname)})")
    if lookup_key is not None:
        if len(lookup_key) > 200:
            issues.append(f"lookup_key exceeds 200 characters ({len(lookup_key)})")
        if not re.fullmatch(r"[A-Za-z0-9_.\-:]+", lookup_key):
            issues.append("lookup_key should contain only alphanumeric characters, underscores, dots, colons, or hyphens")
    issues.extend(validate_metadata(metadata))
    return issues


def detect_stripe_cli() -> dict[str, Any]:
    """Check whether the optional official Stripe CLI binary is present on PATH."""
    binary = shutil.which("stripe")
    if not binary:
        return {"installed": False, "path": None, "version": None}
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ver = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else None
        return {"installed": proc.returncode == 0, "path": binary, "version": ver}
    except Exception:
        return {"installed": False, "path": binary, "version": None}


class StripeClient:
    """Minimal, dependency-free HTTPS client for Stripe API v1."""

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_version: str | None = None,
        stripe_account: str | None = None,
        timeout: int = 30,
        allow_loopback_http: bool = False,
    ) -> None:
        info = classify_key(secret_key)
        if not info["present"]:
            raise StripeError("Missing STRIPE_SECRET_KEY. Set STRIPE_SECRET_KEY in your environment before running live commands.")
        if not info["valid_secret"]:
            raise StripeError(
                f"Invalid key type '{info['kind']}' (prefix '{info['prefix']}'). "
                "STRIPE_SECRET_KEY must be a secret key (sk_test_ / sk_live_) or restricted key (rk_test_ / rk_live_), "
                "never a publishable key (pk_) or webhook secret (whsec_)."
            )
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS:
            pass
        elif allow_loopback_http and parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "localhost"):
            pass
        else:
            raise StripeError(f"Untrusted Stripe API base_url: {base_url}")
        self.secret_key = secret_key.strip()
        self.key_info = info
        self.mode = info["mode"]
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.stripe_account = stripe_account
        self.timeout = timeout

    def require_mutation_gate(self, *, allow_live: bool = False) -> None:
        """Refuse live-mode mutations unless --live was explicitly provided."""
        if self.mode == "live" and not allow_live:
            raise StripeError(
                "Refusing mutation on a LIVE Stripe key (sk_live_ / rk_live_) without explicit --live flag. "
                "Preview first, verify the plan_sha256, and pass --apply --confirm-plan <sha256> --live when authorized."
            )

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request against Stripe API v1."""
        method_u = method.upper()
        rel = path if path.startswith("/") else "/" + path
        url = self.base_url + rel
        body: bytes | None = None
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "User-Agent": f"legends-stripe/{__version__} (python/{sys.version_info.major}.{sys.version_info.minor})",
            "Accept": "application/json",
        }
        if self.api_version:
            headers["Stripe-Version"] = self.api_version
        if self.stripe_account:
            headers["Stripe-Account"] = self.stripe_account
        if method_u in ("GET", "DELETE"):
            if params:
                query = urllib.parse.urlencode(flatten_stripe_params(params), doseq=True)
                url = f"{url}{'&' if '?' in url else '?'}{query}"
        else:
            body = encode_stripe_form(params)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key

        req = urllib.request.Request(url, data=body, headers=headers, method=method_u)
        opener = build_safe_opener()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise StripeError(f"Stripe API response exceeded {MAX_RESPONSE_BYTES} bytes")
                raw = raw_bytes.decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return data
        except urllib.error.HTTPError as exc:
            raw_err = exc.read(65536).decode("utf-8", errors="replace") if exc.fp else ""
            req_id = exc.headers.get("Request-Id") if exc.headers else None
            try:
                parsed_err = json.loads(raw_err).get("error", {})
            except Exception:
                parsed_err = {}
            msg = scrub_secrets(parsed_err.get("message") or f"Stripe HTTP {exc.code} on {method_u} {rel}")
            raise StripeError(
                msg,
                status=exc.code,
                code=parsed_err.get("code") or parsed_err.get("type"),
                param=parsed_err.get("param"),
                request_id=req_id,
            ) from None
        except urllib.error.URLError as exc:
            raise StripeError(f"Network error contacting Stripe API: {scrub_secrets(str(exc.reason))}") from None

    def list_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int = 250,
    ) -> list[dict[str, Any]]:
        """Auto-paginate a Stripe list endpoint up to max_items."""
        collected: list[dict[str, Any]] = []
        base_params = dict(params or {})
        starting_after: str | None = None
        while len(collected) < max_items:
            page_limit = min(100, max_items - len(collected))
            query = {**base_params, "limit": page_limit}
            if starting_after:
                query["starting_after"] = starting_after
            page = self.request("GET", path, query)
            rows = page.get("data", [])
            if not isinstance(rows, list) or not rows:
                break
            collected.extend(rows)
            if not page.get("has_more"):
                break
            last_id = rows[-1].get("id")
            if not last_id:
                break
            starting_after = last_id
        return collected[:max_items]
