"""Pure Python 3.11+ standard-library client for Stripe API v1 with form-encoding, key-mode safety, and plan hashing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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

SOFT_LAND_WEBHOOK_EVENTS = (
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


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
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise StripeError(f"Requested env file does not exist: {target}")
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return data
        except urllib.error.HTTPError as exc:
            raw_err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            req_id = exc.headers.get("Request-Id") if exc.headers else None
            try:
                parsed_err = json.loads(raw_err).get("error", {})
            except Exception:
                parsed_err = {}
            msg = parsed_err.get("message") or f"Stripe HTTP {exc.code} on {method_u} {rel}"
            raise StripeError(
                msg,
                status=exc.code,
                code=parsed_err.get("code") or parsed_err.get("type"),
                param=parsed_err.get("param"),
                request_id=req_id,
            ) from None
        except urllib.error.URLError as exc:
            raise StripeError(f"Network error contacting Stripe API: {exc.reason}") from None

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
